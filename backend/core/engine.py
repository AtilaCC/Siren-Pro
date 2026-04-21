"""
core/engine.py — Loop principal do bot SIREN PRO.

Orquestra todo o ciclo de análise:
  1. Contexto BTC
  2. Busca de tokens Alpha
  3. Filtro anti-scam
  4. Enriquecimento (RSI real, MA, FR, pré-pump)
  5. Snapshot
  6. Verificação de alertas 24h
  7. Detecção de narrativas (Claude)
  8. Envio de alertas Telegram
  9. Resumos matinal / semanal
  10. Backtest avançado (a cada 10 ciclos)
"""

import os
import time
import asyncio
import logging
from datetime import datetime

from core.scanner import (
    fetch_btc_context, fetch_alpha_tokens, enrich_tokens,
    build_token, passes_quality_filter, save_snapshot,
)
from core.scoring import (
    get_adaptive_weights, update_adaptive_weights,
    calc_alert_priority, get_btc_context,
    passes_entry_quality,
)
from ai.claude import (
    claude_analyze_token, claude_validate_alert,
    claude_learn_from_results, claude_detect_narratives,
)
from db.connection import get_db
from trading.executor import execute_signal_async

# Sinais que abrem trade automaticamente (paper mode por padrão)
AUTO_TRADE_LABELS = set(os.environ.get("AUTO_TRADE_LABELS", "pump,pre,rsi,stier,gc").split(","))
AUTO_TRADE_MIN_SCORE = int(os.environ.get("AUTO_TRADE_MIN_SCORE", "75"))

# ── Anti-overtrading ──────────────────────────────────────────────────────────
MAX_SIMULTANEOUS_TRADES = int(os.environ.get("MAX_SIMULTANEOUS_TRADES", "4"))
TRADE_COOLDOWN_SECONDS  = int(os.environ.get("TRADE_COOLDOWN_SECONDS", "3600"))  # 1h por símbolo

# Estado em memória — persiste durante o processo
_open_trade_symbols:  set[str] = set()   # símbolos com posição aberta
_last_entry_ts:       dict[str, float] = {}  # sym → timestamp da última entrada

import threading
_trade_state_lock = threading.Lock()


# ═══════════════════════════════════════
# ANTI-OVERTRADING — gate functions
# ═══════════════════════════════════════

def _can_open_trade(sym: str, t: dict) -> tuple[bool, str]:
    """
    Portão completo antes de qualquer execute_signal_async().
    Retorna (True, '') ou (False, motivo).

    Verificações em ordem de custo (mais rápidas primeiro):
      1. Limite de posições simultâneas
      2. Cooldown por símbolo
      3. Quality gate (score, RSI, volume, contexto BTC)
    """
    with _trade_state_lock:
        # 1. Cap de posições abertas
        if len(_open_trade_symbols) >= MAX_SIMULTANEOUS_TRADES:
            return False, f"max_trades:{len(_open_trade_symbols)}/{MAX_SIMULTANEOUS_TRADES}"

        # 2. Cooldown — evita re-entrada rápida no mesmo ativo
        last = _last_entry_ts.get(sym, 0)
        elapsed = time.time() - last
        if elapsed < TRADE_COOLDOWN_SECONDS:
            remaining = int(TRADE_COOLDOWN_SECONDS - elapsed)
            return False, f"cooldown:{sym}:{remaining}s restantes"

    # 3. Quality gate (fora do lock — não usa estado mutável)
    ok, reason = passes_entry_quality(t)
    if not ok:
        return False, f"quality_gate:{reason}"

    return True, ""


def _register_trade_open(sym: str) -> None:
    """Registra abertura de posição. Chamado APÓS execute_signal_async bem-sucedido."""
    with _trade_state_lock:
        _open_trade_symbols.add(sym)
        _last_entry_ts[sym] = time.time()


def _register_trade_close(sym: str) -> None:
    """
    Remove símbolo das posições abertas.
    Chamado pelo position_monitor ou manualmente quando a posição fecha.
    """
    with _trade_state_lock:
        _open_trade_symbols.discard(sym)


def get_trade_state() -> dict:
    """Snapshot do estado atual para a API (routes.py pode expor isso)."""
    with _trade_state_lock:
        return {
            "open_positions":   list(_open_trade_symbols),
            "open_count":       len(_open_trade_symbols),
            "max_simultaneous": MAX_SIMULTANEOUS_TRADES,
            "cooldown_seconds": TRADE_COOLDOWN_SECONDS,
            "last_entries":     {k: int(v) for k, v in _last_entry_ts.items()},
        }

log = logging.getLogger("SIREN.engine")

TG_TOKEN         = os.environ.get("TG_TOKEN", "")
TG_CHAT          = os.environ.get("TG_CHAT", "")
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", 30))
ALERT_COOLDOWN   = int(os.environ.get("ALERT_COOLDOWN", 600))
ALERT_MIN_SCORE  = int(os.environ.get("ALERT_MIN_SCORE", 75))


# ═══════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════

async def tg_send(session, text: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    url     = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    try:
        async with session.post(url, json=payload,
                                timeout=__import__("aiohttp").ClientTimeout(total=10)) as r:
            if r.status != 200:
                log.warning(f"TG erro: {r.status}")
    except Exception as e:
        log.error(f"TG falha: {e}")


def can_send(key: str) -> bool:
    """Cooldown anti-spam por chave. Persiste no banco."""
    db  = get_db()
    c   = db.cursor()
    c.execute("SELECT ts FROM spam WHERE key=%s", (key,))
    row = c.fetchone()
    now = int(time.time())
    if row and now - row[0] < ALERT_COOLDOWN:
        db.close()
        return False
    c.execute(
        """INSERT INTO spam (key, ts) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET ts = EXCLUDED.ts""",
        (key, now),
    )
    db.commit()
    db.close()
    return True


# ═══════════════════════════════════════
# FORMATADORES
# ═══════════════════════════════════════

def fmt_price(n):
    if not n:       return "$0"
    if n >= 1000:   return f"${n:,.0f}"
    if n >= 1:      return f"${n:.4f}"
    if n >= 0.01:   return f"${n:.6f}"
    if n >= 0.0001: return f"${n:.8f}"
    return f"${n:.2e}"


def fmt_vol(v):
    if v >= 1e9: return f"{v/1e9:.1f}B"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return f"{v:.0f}"


# ═══════════════════════════════════════
# SAVE / VERIFY ALERTAS
# ═══════════════════════════════════════

def save_alert(t: dict, label: str, priority: int = 5):
    db = get_db()
    db.cursor().execute(
        """INSERT INTO alerts (ts, sym, price, label, score, rsi, priority)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (int(time.time()), t["sym"], t["price"], label, t["score"], t["rsi"], priority),
    )
    db.commit()
    db.close()


async def verify_alerts(session, tokens: list) -> list:
    """Verifica alertas com 24h de vida e calcula hit/miss."""
    db = get_db()
    c  = db.cursor()
    c.execute(
        """SELECT id, sym, price, label, score, rsi FROM alerts
           WHERE verified=0 AND ts <= %s""",
        (int(time.time()) - 86400,),
    )
    pending = c.fetchall()
    if not pending:
        db.close()
        return []

    token_map = {t["sym"]: t for t in tokens}
    results   = []
    hits      = 0

    for alert_id, sym, entry_price, label, score, rsi in pending:
        tok = token_map.get(sym)
        if not tok:
            continue
        pct = (tok["price"] - entry_price) / entry_price * 100 if entry_price > 0 else 0
        hit = (
            ("PUMP"     in label and pct > 0)
            or ("DUMP"  in label and pct < 0)
            or ("RSI"   in label and pct > 5)
            or ("S-TIER" in label and pct > 0)
            or ("GC"    in label and pct > 0)
            or ("PRÉ"   in label and pct > 10)
            or ("REVERSÃO" in label and pct > 5)
        )
        c.execute(
            """UPDATE alerts SET verified=1, verified_at=%s, exit_price=%s,
               pct_change=%s, hit=%s WHERE id=%s""",
            (int(time.time()), tok["price"], round(pct, 2), int(hit), alert_id),
        )
        if hit:
            hits += 1
        results.append({
            "sym": sym, "label": label, "pct": round(pct, 1),
            "hit": hit, "score": score, "rsi": rsi,
        })

    db.commit()

    if results:
        rate  = round(hits / len(results) * 100)
        top3  = sorted(results, key=lambda x: x["pct"], reverse=True)[:3]
        top_t = "\n".join([
            f"{'✅' if r['hit'] else '❌'} ${r['sym']} {r['label']}: {r['pct']:+.1f}%"
            for r in top3
        ])
        c.execute("SELECT COUNT(*), SUM(hit), AVG(pct_change) FROM alerts WHERE verified=1 AND pct_change BETWEEN -200 AND 500")
        row = c.fetchone()
        t_all, h_all, avg_all = row[0] or 0, row[1] or 0, row[2] or 0
        r_all = round(h_all / t_all * 100) if t_all > 0 else 0
        db.close()

        await tg_send(
            session,
            f"📊 <b>VERIFICAÇÃO 24h — SIREN PRO</b>\n\n"
            f"Verificados: <b>{len(results)}</b> | Acertos: <b>{hits} ({rate}%)</b>\n\n"
            f"Top resultados:\n{top_t}\n\n"
            f"📈 Acumulado: <b>{h_all}/{t_all} ({r_all}%)</b> | Média: <b>{avg_all:+.1f}%</b>\n\n"
            f"⚡ SIREN PRO",
        )
        asyncio.create_task(claude_learn_from_results(results))
    else:
        db.close()

    return results


# ═══════════════════════════════════════
# ENVIO DE ALERTAS
# ═══════════════════════════════════════

async def send_alerts(session, tokens: list, narrative: dict = None):
    if not TG_TOKEN or not TG_CHAT:
        return

    sent_count = skipped_low = skipped_ai = 0
    btc_ctx    = get_btc_context()

    narr_ctx = ""
    if narrative and narrative.get("dominant_narrative"):
        narr_ctx = f"\n🧠 Narrativa: <b>{narrative['dominant_narrative']}</b>"
        if narrative.get("hot_chain"):
            narr_ctx += f" · {narrative['hot_chain'].upper()}"

    btc_emoji = "📈" if btc_ctx["trend"] == "bullish" else "📉" if btc_ctx["trend"] == "bearish" else "➡️"
    btc_line  = f"{btc_emoji} BTC {btc_ctx['trend'].upper()} {btc_ctx.get('chg_4h', 0):+.1f}% (4h)"
    SEP       = "━━━━━━━━━━━━━━━"

    _LABEL_MAP = {
        "pump": "PUMP", "dump": "DUMP", "rsi": "RSI OVERSOLD",
        "stier": "S-TIER", "gc": "GOLDEN CROSS", "whale": "BALEIA",
        "pre": "PRÉ-PUMP", "rev": "REVERSÃO",
    }

    for t in tokens:
        sym     = t["sym"]
        chain   = (t.get("chain") or "").upper() or "?"
        rsi_tag = "✓" if t["rsi_real"] else "~"
        fr_line = (
            f"FR: <b>{t['fr']:+.4f}%</b> {'🔥 SQUEEZE' if t['fr'] < -0.05 else ''}"
            if t["fr_real"] else ""
        )
        holders  = f"{t['holders']:,}" if t.get("holders") else "?"
        vol_line = f"Vol: <b>${fmt_vol(t['vol'])}</b>"

        def build_msg(emoji, title, body_lines):
            lines = [f"{emoji} <b>{title} — ${sym}</b>", SEP] + body_lines + [SEP, btc_line]
            if fr_line: lines.append(fr_line)
            if narr_ctx and t["score"] >= 70: lines.append(narr_ctx)
            lines += [f"🔗 {chain} · 👥 {holders} holders", f"#BinanceAlpha ⚡ SIREN PRO"]
            return "\n".join(lines)

        alerts_to_check = []

        if t["chg"] >= 10:
            body = [
                f"💰 {fmt_price(t['price'])} | 📈 <b>{t['chg']:+.1f}%</b>",
                f"RSI{rsi_tag}: {t['rsi']} | Score: <b>{t['tier']}-{t['score']}</b>", vol_line,
            ]
            alerts_to_check.append(("pump", "🚀 PUMP", build_msg("🚀", "PUMP", body), "🚀 PUMP"))

        if t["chg"] <= -10:
            body = [
                f"💰 {fmt_price(t['price'])} | 📉 <b>{t['chg']:+.1f}%</b>",
                f"RSI{rsi_tag}: {t['rsi']} | Score: {t['tier']}-{t['score']}", vol_line,
            ]
            alerts_to_check.append(("dump", "💥 DUMP", build_msg("💥", "DUMP", body), "💥 DUMP"))

        if t["rsi"] < 30:
            body = [
                f"💰 {fmt_price(t['price'])} | {t['chg']:+.1f}%",
                f"RSI{rsi_tag}: <b>{t['rsi']}</b> ← OVERSOLD",
                f"Score: {t['tier']}-{t['score']} | {vol_line}",
            ]
            alerts_to_check.append(("rsi", "💎 RSI OVERSOLD", build_msg("💎", "RSI OVERSOLD", body), "💎 RSI OVERSOLD"))

        if t["tier"] == "S":
            body = [
                f"💰 {fmt_price(t['price'])} | {t['chg']:+.1f}%",
                f"RSI{rsi_tag}: {t['rsi']} | Score: <b>S-{t['score']}</b>", vol_line,
            ]
            alerts_to_check.append(("stier", "🔮 S-TIER", build_msg("🔮", "S-TIER", body), "🔮 S-TIER"))

        if t["gc"] and t["gc_real"]:
            body = [
                f"💰 {fmt_price(t['price'])} | {t['chg']:+.1f}%",
                f"MA9: {fmt_price(t['ma9'])} > MA21: {fmt_price(t['ma21'])}",
                f"RSI{rsi_tag}: {t['rsi']} | Score: {t['tier']}-{t['score']}",
            ]
            alerts_to_check.append(("gc", "✨ GOLDEN CROSS", build_msg("✨", "GOLDEN CROSS ✓", body), "✨ GOLDEN CROSS"))

        if t["vol"] > 1e6:
            body = [
                f"💰 {fmt_price(t['price'])} | {t['chg']:+.1f}%",
                f"🐋 {vol_line} ← VOLUME ALTO",
                f"Score: {t['tier']}-{t['score']} | RSI{rsi_tag}: {t['rsi']}",
            ]
            alerts_to_check.append(("whale", "🐋 BALEIA", build_msg("🐋", "BALEIA", body), "🐋 BALEIA"))

        if t.get("pre") and t.get("pre_conf", 0) >= 0.45:
            conf_pct = int(t["pre_conf"] * 100)
            vg       = t.get("vol_growth", 0)
            body     = [
                f"💰 {fmt_price(t['price'])} | Acumulação {conf_pct}% conf",
                f"📊 Vol: <b>+{vg:.0f}%</b> | BB comprimido",
                f"RSI{rsi_tag}: {t['rsi']} | Score: {t['tier']}-{t['score']}",
                f"⚠ Volume cresce, preço ainda flat → possível breakout",
            ]
            alerts_to_check.append(("pre", "🔮 PRÉ-PUMP", build_msg("🔮", "PRÉ-PUMP", body), "🔮 PRÉ-PUMP"))

        if t["rev"]:
            body = [
                f"💰 {fmt_price(t['price'])} | {t['chg']:+.1f}%",
                f"RSI{rsi_tag}: <b>{t['rsi']}</b> ← reversão detectada",
                f"Score: {t['tier']}-{t['score']}",
            ]
            alerts_to_check.append(("rev", "🔄 REVERSÃO", build_msg("🔄", "REVERSÃO", body), "🔄 REVERSÃO"))

        for key, label, msg, label_short in alerts_to_check:
            if not can_send(f"{key}_{sym}"):
                continue
            label_clean = _LABEL_MAP.get(key, label_short)
            priority    = calc_alert_priority(t, label_clean)
            if priority < 5:
                skipped_low += 1
                continue
            if t["score"] < ALERT_MIN_SCORE and "DUMP" not in label and t["score"] < 40:
                skipped_low += 1
                continue
            if not await claude_validate_alert(t, label_short, priority):
                skipped_ai += 1
                continue

            pri_bar   = "🟩" * min(priority, 5) + "⬜" * (5 - min(priority, 5))
            final_msg = msg.replace("⚡ SIREN PRO", f"⚡ SIREN PRO | {pri_bar} {priority}/10")

            await tg_send(session, final_msg)
            save_alert(t, label_short, priority)
            sent_count += 1

            # ── Auto-trade: portão completo + validação IA ──────────────
            if key in AUTO_TRADE_LABELS and t["score"] >= AUTO_TRADE_MIN_SCORE:
                approved, gate_reason = _can_open_trade(sym, t)
                if not approved:
                    log.info(f"[AUTO-TRADE] ⏭ ${sym} bloqueado: {gate_reason}")
                else:
                    try:
                        result = await execute_signal_async(t, label_short)
                        if result["success"]:
                            _register_trade_open(sym)
                            ai_conf = result.get("ai_confidence", 0)
                            ai_reg  = result.get("ai_regime", "?")
                            sl_pct  = result.get("sl_pct", 0)
                            log.info(
                                f"[AUTO-TRADE] ✅ ${sym} {label_short} | "
                                f"mode={result['mode']} size=${result['total_usdt']:.2f} | "
                                f"SL={sl_pct:.1f}% TP=${result['tp_price']:.8f} | "
                                f"IA conf={ai_conf:.2f} regime={ai_reg}"
                            )
                            await tg_send(
                                session,
                                f"🤖 <b>AUTO-TRADE EXECUTADO</b>\n"
                                f"${sym} {label_short}\n"
                                f"💵 ${result['total_usdt']:.2f} USDT\n"
                                f"🛡 SL: {sl_pct:.1f}% | 🎯 TP: ${result['tp_price']:.8f}\n"
                                f"🧠 IA: {ai_conf:.0%} conf | {ai_reg}\n"
                                f"⚡ SIREN PRO"
                            )
                        else:
                            reason = result.get("error", "?")
                            if "IA bloqueou" in reason or "confidence" in reason:
                                log.info(f"[AUTO-TRADE] 🧠 IA BLOQUEOU ${sym}: {reason}")
                            else:
                                log.info(f"[AUTO-TRADE] ⏭ ${sym} ignorado: {reason}")
                    except Exception as et:
                        log.error(f"[AUTO-TRADE] Erro ao executar ${sym}: {et}")

            await asyncio.sleep(0.3)

    log.info(
        f"Alertas: {sent_count} enviados | {skipped_low} baixa prioridade | "
        f"{skipped_ai} bloqueados por IA"
    )


# ═══════════════════════════════════════
# BACKTEST AVANÇADO
# ═══════════════════════════════════════

def run_advanced_backtest(label: str = None) -> dict | None:
    """
    Backtest completo sobre alertas verificados.
    Calcula: win rate, profit factor, sharpe, streaks, breakdown por RSI/tier.
    """
    db = get_db()
    c  = db.cursor()

    q      = "SELECT a.id, a.sym, a.ts, a.price, a.label, a.score, a.rsi FROM alerts a WHERE a.verified=1"
    params = []
    if label:
        q += " AND a.label LIKE %s"
        params.append(f"%{label}%")
    q += " ORDER BY a.ts"
    c.execute(q, params)
    alerts = c.fetchall()

    if len(alerts) < 10:
        db.close()
        return None

    trades = []
    for alert_id, sym, ts, entry_price, lbl, score, rsi in alerts:
        c.execute(
            """SELECT ts, price FROM snapshots
               WHERE sym=%s AND ts > %s ORDER BY ts LIMIT 48""",
            (sym, ts),
        )
        snaps = c.fetchall()
        if not snaps:
            continue

        prices    = [entry_price] + [s[1] for s in snaps]
        max_price = max(prices)
        min_price = min(prices)
        final     = prices[-1]
        max_gain  = (max_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        max_dd    = (entry_price - min_price) / entry_price * 100 if entry_price > 0 else 0
        final_pct = (final - entry_price) / entry_price * 100     if entry_price > 0 else 0

        bars_to_target = None
        for i, (_, snap_price) in enumerate(snaps):
            if snap_price >= entry_price * 1.10:
                bars_to_target = i + 1
                break

        c.execute(
            "UPDATE alerts SET max_gain=%s, max_drawdown=%s, bars_to_target=%s WHERE id=%s",
            (round(max_gain, 2), round(max_dd, 2), bars_to_target or 0, alert_id),
        )

        tier = "S" if score >= 80 else "A" if score >= 65 else "B" if score >= 50 else "C"
        trades.append({
            "pct": final_pct, "max_gain": max_gain, "max_dd": max_dd,
            "bars": bars_to_target, "hit": final_pct > 0,
            "score": score, "rsi": rsi or 50, "tier": tier, "ts": ts,
        })

    db.commit()
    db.close()

    if not trades:
        return None

    n     = len(trades)
    wins  = [t for t in trades if t["hit"]]
    loses = [t for t in trades if not t["hit"]]
    rets  = [t["pct"] for t in trades]

    avg_ret   = sum(rets) / n
    win_rate  = len(wins) / n * 100
    gross_win = sum(t["pct"] for t in wins)
    gross_los = abs(sum(t["pct"] for t in loses)) if loses else 1
    pf        = min(gross_win / gross_los if gross_los > 0 else 999.0, 999.0)
    avg_gain  = sum(t["max_gain"] for t in trades) / n
    avg_dd    = sum(t["max_dd"]   for t in trades) / n
    avg_bars  = (
        sum(t["bars"] or 0 for t in trades if t["bars"])
        / max(1, sum(1 for t in trades if t["bars"]))
    )

    mean_r = avg_ret / 100
    std_r  = (sum((r / 100 - mean_r) ** 2 for r in rets) / n) ** 0.5
    sharpe = round(mean_r / std_r * (252 ** 0.5), 2) if std_r > 0 else 0

    rsi_zones = {
        "oversold":   [t for t in trades if t["rsi"] < 35],
        "neutral":    [t for t in trades if 35 <= t["rsi"] < 60],
        "overbought": [t for t in trades if t["rsi"] >= 60],
    }
    rsi_breakdown = {
        z: {"n": len(zt), "win_rate": round(sum(1 for t in zt if t["hit"]) / len(zt) * 100, 1)}
        for z, zt in rsi_zones.items() if zt
    }

    tier_breakdown = {}
    for tier in ["S", "A", "B", "C"]:
        tt = [t for t in trades if t["tier"] == tier]
        if tt:
            tier_breakdown[tier] = {
                "n": len(tt),
                "win_rate": round(sum(1 for t in tt if t["hit"]) / len(tt) * 100, 1),
            }

    max_win_streak = max_lose_streak = cur_win = cur_lose = 0
    for t in trades:
        if t["hit"]:
            cur_win += 1; cur_lose = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_lose += 1; cur_win = 0
            max_lose_streak = max(max_lose_streak, cur_lose)

    result = {
        "label": label or "ALL", "n_trades": n,
        "win_rate": round(win_rate, 1), "avg_return": round(avg_ret, 2),
        "avg_max_gain": round(avg_gain, 2), "avg_drawdown": round(avg_dd, 2),
        "avg_bars_to_target": round(avg_bars, 1), "profit_factor": round(pf, 2),
        "sharpe": sharpe, "max_win_streak": max_win_streak,
        "max_lose_streak": max_lose_streak,
        "rsi_breakdown": rsi_breakdown, "tier_breakdown": tier_breakdown,
    }

    db2 = get_db()
    db2.cursor().execute(
        """INSERT INTO backtest_results
           (ts, label, n_trades, win_rate, avg_return, avg_max_gain, avg_drawdown,
            avg_bars_to_target, profit_factor, sharpe)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            int(time.time()), result["label"], n, result["win_rate"],
            result["avg_return"], result["avg_max_gain"], result["avg_drawdown"],
            result["avg_bars_to_target"], result["profit_factor"], result["sharpe"],
        ),
    )
    db2.commit()
    db2.close()

    log.info(
        f"Backtest [{label or 'ALL'}]: WR={win_rate:.0f}% PF={pf:.2f} "
        f"Sharpe={sharpe:.2f} n={n} | Streaks: W{max_win_streak}/L{max_lose_streak}"
    )
    return result


# ═══════════════════════════════════════
# RESUMOS
# ═══════════════════════════════════════

async def morning_summary(session, tokens: list, narrative: dict = None):
    now = datetime.now()
    if now.hour != 8:
        return
    if not can_send(f"morning_{now.strftime('%Y%m%d')}"):
        return

    top5    = sorted(tokens, key=lambda t: t["score"], reverse=True)[:5]
    top_txt = "\n".join([
        f"{i+1}. ${t['sym']} {t['tier']}-{t['score']} | {fmt_price(t['price'])} | {t['chg']:+.1f}%"
        for i, t in enumerate(top5)
    ])

    bt     = run_advanced_backtest()
    bt_txt = ""
    if bt:
        bt_txt = (
            f"\n\n📊 <b>Backtest Geral</b> (n={bt['n_trades']}):\n"
            f"WR: <b>{bt['win_rate']}%</b> | PF: {bt['profit_factor']} | Sharpe: {bt['sharpe']}\n"
            f"Avg gain: +{bt['avg_max_gain']}% | Avg DD: -{bt['avg_drawdown']}%\n"
            f"Sequências: 🟩{bt.get('max_win_streak',0)}W / 🟥{bt.get('max_lose_streak',0)}L"
        )
        for zone, data in bt.get("rsi_breakdown", {}).items():
            emoji = "💎" if zone == "oversold" else "➡️" if zone == "neutral" else "⚠️"
            bt_txt += f"\n  {emoji} {zone}: WR={data['win_rate']}% (n={data['n']})"
        tier_lines = [
            f"  {tier}: WR={data['win_rate']}% (n={data['n']})"
            for tier, data in bt.get("tier_breakdown", {}).items()
        ]
        if tier_lines:
            bt_txt += "\nTier WR: " + " | ".join(tier_lines)

    bt_labels_txt = ""
    for lbl in ["PUMP", "PRÉ-PUMP", "RSI", "S-TIER"]:
        bl = run_advanced_backtest(lbl)
        if bl and bl["n_trades"] >= 5:
            bt_labels_txt += f"\n  {lbl}: WR={bl['win_rate']}% PF={bl['profit_factor']} n={bl['n_trades']}"
    if bt_labels_txt:
        bt_txt += f"\n\n📌 Por sinal:{bt_labels_txt}"

    w     = get_adaptive_weights()
    w_txt = f"\n⚙️ Pesos: chg={w['chg']} rsi={w['rsi']} vm={w['vm']} fr={w['fr']}"

    narr_txt = ""
    if narrative and narrative.get("dominant_narrative"):
        narr_txt = (
            f"\n\n🧠 <b>{narrative['dominant_narrative']}</b> · "
            f"{narrative.get('hot_chain','?').upper()}\n{narrative.get('insight','')}"
        )

    btc_ctx = get_btc_context()
    btc_txt = (
        f"\n\n₿ BTC: <b>{btc_ctx.get('trend','?').upper()}</b> | "
        f"{btc_ctx.get('chg_4h',0):+.1f}% 4h | RSI {btc_ctx.get('rsi',50):.0f}"
    )

    db  = get_db()
    c   = db.cursor()
    c.execute("SELECT COUNT(*), SUM(hit), AVG(pct_change) FROM alerts WHERE verified=1 AND pct_change BETWEEN -200 AND 500")
    row = c.fetchone()
    db.close()
    v_total, v_hits, v_avg = row[0] or 0, row[1] or 0, row[2] or 0
    v_rate  = round(v_hits / v_total * 100) if v_total > 0 else 0
    v_txt   = (
        f"\n\n📈 Acertos: <b>{v_hits}/{v_total} ({v_rate}%)</b> | Média: {v_avg:+.1f}%"
        if v_total > 0 else ""
    )

    await tg_send(
        session,
        f"☀️ <b>RESUMO MATINAL — SIREN PRO</b>\n"
        f"{now.strftime('%A, %d/%m/%Y')}\n\n"
        f"🏆 TOP 5:\n{top_txt}"
        f"{btc_txt}{narr_txt}{v_txt}{bt_txt}{w_txt}\n\n"
        f"⚡ SIREN PRO Hedge Fund Engine",
    )


async def weekly_summary(session, tokens: list):
    now = datetime.now()
    if now.weekday() != 6 or now.hour != 9:
        return
    if not can_send(f"weekly_{now.strftime('%Y%m%d')}"):
        return

    week_ago = int(time.time()) - 7 * 86400
    db = get_db()
    c  = db.cursor()
    c.execute("SELECT COUNT(*) FROM alerts WHERE ts>=%s", (week_ago,))
    total = c.fetchone()[0]
    c.execute(
        "SELECT COUNT(*), SUM(hit), AVG(pct_change) FROM alerts WHERE verified=1 AND verified_at>=%s",
        (week_ago,),
    )
    row = c.fetchone()
    c.execute("SELECT COUNT(DISTINCT ts) FROM snapshots")
    snaps = c.fetchone()[0]
    db.close()

    v_total, v_hits, v_avg = row[0] or 0, row[1] or 0, row[2] or 0
    v_rate = round(v_hits / v_total * 100) if v_total > 0 else 0

    bt_labels = []
    for lbl in ["PUMP", "PRÉ-PUMP", "RSI", "REVERSÃO"]:
        bt = run_advanced_backtest(lbl)
        if bt:
            bt_labels.append(f"  {lbl}: WR={bt['win_rate']}% PF={bt['profit_factor']} n={bt['n_trades']}")

    await tg_send(
        session,
        f"📊 <b>RELATÓRIO SEMANAL — SIREN PRO</b>\n\n"
        f"🔔 Alertas: {total}\n"
        f"✅ Verificados: {v_hits}/{v_total} ({v_rate}%) | Média: {v_avg:+.1f}%\n"
        f"📦 Snapshots: {snaps}\n\n"
        f"📊 Backtest por sinal:\n" + "\n".join(bt_labels) + "\n\n"
        f"⚡ SIREN PRO",
    )


import aiohttp as _aiohttp

BSCSCAN_KEY = os.environ.get("BSCSCAN_API_KEY", "")

async def fetch_onchain_signal(session, token: dict) -> dict:
    if not BSCSCAN_KEY or token.get("chain", "").lower() not in ["bsc", "bnb"]:
        return {}
    contract = token.get("contract", "")
    if not contract or len(contract) < 20:
        return {}
    try:
        import aiohttp
        url = (
            f"https://api.bscscan.com/api?module=account&action=tokentx"
            f"&contractaddress={contract}&page=1&offset=100&sort=desc"
            f"&apikey={BSCSCAN_KEY}"
        )
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return {}
            data = await r.json()
            if data.get("status") != "1":
                return {}
            txs = data.get("result", [])
            large_txs = 0
            unique_buyers = set()
            for tx in txs[:50]:
                try:
                    value = float(tx.get("value", 0)) / (10 ** int(tx.get("tokenDecimal", 18)))
                    if value > 10000:
                        large_txs += 1
                        unique_buyers.add(tx.get("to", ""))
                except Exception:
                    continue
            signal = {
                "large_txs": large_txs,
                "unique_whale_buyers": len(unique_buyers),
                "whale_signal": large_txs >= 3,
            }
            if signal["whale_signal"]:
                log.info(f"BSCScan whale: ${token['sym']} {large_txs} large txs")
            return signal
    except Exception as e:
        log.warning(f"BSCScan falha: {e}")
        return {}


# ═══════════════════════════════════════
# CICLO PRINCIPAL
# ═══════════════════════════════════════

async def run_cycle(session, cycle_count: int = 0) -> list:
    log.info("═" * 50)
    log.info(f"Ciclo #{cycle_count} — SIREN PRO")

    await fetch_btc_context(session)

    weights = get_adaptive_weights()
    if cycle_count % 10 == 0 and cycle_count > 0:
        new_w = update_adaptive_weights()
        if new_w:
            weights = new_w

    raw = await fetch_alpha_tokens(session)
    if not raw:
        log.warning("Sem dados Alpha")
        return []

    tokens = [build_token(d, weights) for d in raw]
    tokens = [t for t in tokens if t and t["price"] > 0]

    before = len(tokens)
    tokens = [t for t in tokens if passes_quality_filter(t)[0]]
    log.info(f"Anti-scam: {before - len(tokens)} removidos | {len(tokens)} restantes")

    tokens.sort(key=lambda t: t["score"], reverse=True)
    tokens = await enrich_tokens(session, tokens, weights)

    save_snapshot(tokens)
    await verify_alerts(session, tokens)

    narrative = {}
    if cycle_count % 4 == 0:
        narrative = await claude_detect_narratives(tokens)
        if narrative.get("dominant_narrative"):
            log.info(f"Narrativa: {narrative['dominant_narrative']} | chain: {narrative.get('hot_chain')}")

    await send_alerts(session, tokens, narrative)
    await morning_summary(session, tokens, narrative)
    await weekly_summary(session, tokens)

    s_tier   = sum(1 for t in tokens if t["tier"] == "S")
    real_rsi = sum(1 for t in tokens if t["rsi_real"])
    real_fr  = sum(1 for t in tokens if t["fr_real"])
    log.info(f"S-Tier:{s_tier} | RSI real:{real_rsi} | FR real:{real_fr} | BTC:{get_btc_context()['trend']}")
    log.info("Ciclo completo ✅")
    return tokens


async def run_bot():
    """Loop infinito do bot."""
    import aiohttp
    log.info("🚀 SIREN PRO iniciando...")
    log.info(f"TG: {'✅' if TG_TOKEN else '❌'} | Intervalo: {INTERVAL_MINUTES}min")

    cycle = 0
    async with aiohttp.ClientSession() as session:
        if TG_TOKEN and TG_CHAT:
            await tg_send(
                session,
                "🚀 <b>SIREN PRO ONLINE</b>\n\n"
                "✅ Score Adaptativo\n✅ Funding Rate\n✅ Pré-Pump Avançado\n"
                "✅ Contexto BTC\n✅ Filtro Anti-Scam\n✅ Backtest Institucional\n"
                "✅ Auto Trade (Binance)\n✅ PostgreSQL\n✅ IA Claude\n"
                "✅ Validação IA antes de cada trade\n\n"
                "⚡ SIREN PRO Hedge Fund Engine",
            )

        while True:
            try:
                await run_cycle(session, cycle)
                cycle += 1
            except Exception as e:
                log.error(f"Erro no ciclo: {e}", exc_info=True)
            log.info(f"Próximo ciclo em {INTERVAL_MINUTES} min...")
            await asyncio.sleep(INTERVAL_MINUTES * 60)
