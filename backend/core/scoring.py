"""
core/scoring.py — Score adaptativo, pesos e cálculo de indicadores técnicos.

Preserva toda a lógica do SIREN v6:
  - calc_score()       : pontuação multi-fator com pesos adaptativos
  - get_adaptive_weights() : busca pesos do banco
  - update_adaptive_weights() : ajusta pesos via correlação com acertos
  - calc_rsi(), calc_ma()  : indicadores técnicos
  - calc_alert_priority()  : priorização de alertas
"""

import time
import logging
from db.connection import get_db

log = logging.getLogger("SIREN.scoring")

# ── Estado global BTC (atualizado por scanner) ────────────────────────────
import threading
_btc_context: dict = {"trend": "neutral", "score_mult": 1.0, "rsi": 50, "chg_4h": 0}
_btc_lock = threading.Lock()


def get_btc_context() -> dict:
    with _btc_lock:
        return dict(_btc_context)


def set_btc_context(ctx: dict):
    with _btc_lock:
        _btc_context.update(ctx)


# ═══════════════════════════════════════
# INDICADORES TÉCNICOS
# ═══════════════════════════════════════

def calc_rsi(closes, period=14):
    """RSI clássico com suavização Wilder."""
    if not closes or len(closes) < period + 1:
        return None
    gains = losses = 0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses += abs(d)
    avg_gain = gains / period
    avg_loss = losses / period
    for j in range(period + 1, len(closes)):
        d = closes[j] - closes[j - 1]
        avg_gain = (avg_gain * (period - 1) + (d if d > 0 else 0)) / period
        avg_loss = (avg_loss * (period - 1) + (abs(d) if d < 0 else 0)) / period
    if avg_loss == 0:
        return 100
    return round(100 - (100 / (1 + avg_gain / avg_loss)))


def calc_ma(closes, period):
    """Média simples."""
    if not closes or len(closes) < period:
        return None
    return sum(closes[-period:]) / period


# ═══════════════════════════════════════
# PESOS ADAPTATIVOS
# ═══════════════════════════════════════

def get_adaptive_weights() -> dict:
    """Busca os pesos adaptativos mais recentes do banco."""
    defaults = {k: 1.0 for k in ["chg", "rsi", "vm", "vol", "fr", "holders", "liq"]}
    try:
        db = get_db()
        c  = db.cursor()
        c.execute(
            """SELECT w_chg, w_rsi, w_vm, w_vol, w_fr, w_holders, w_liq
               FROM score_weights ORDER BY ts DESC LIMIT 1"""
        )
        row = c.fetchone()
        db.close()
        if not row:
            return defaults
        keys = ["chg", "rsi", "vm", "vol", "fr", "holders", "liq"]
        return {k: max(0.2, min(2.0, v)) for k, v in zip(keys, row)}
    except Exception as e:
        log.warning(f"get_adaptive_weights fallback: {e}")
        return defaults


def update_adaptive_weights():
    """
    Recalcula os pesos usando correlação de Pearson entre os indicadores
    e o campo 'hit' dos alertas verificados.
    """
    db = get_db()
    c  = db.cursor()

    c.execute(
        """
        SELECT a.sym, a.score, a.rsi, a.hit,
               s.chg, s.vm, s.vol, s.funding_rate, s.holders, s.liq
        FROM alerts a
        JOIN snapshots s ON s.sym = a.sym
            AND s.ts = (SELECT MAX(ts) FROM snapshots WHERE sym=a.sym AND ts <= a.ts)
        WHERE a.verified=1
        ORDER BY a.ts DESC LIMIT 200
        """
    )
    rows = c.fetchall()
    db.close()

    if len(rows) < 30:
        log.info(f"Score adaptativo: dados insuficientes ({len(rows)}/30)")
        return

    def corr(vals, hits):
        n = len(vals)
        if n < 2:
            return 0
        mx = sum(vals) / n
        mh = sum(hits) / n
        num = sum((v - mx) * (h - mh) for v, h in zip(vals, hits))
        dx  = sum((v - mx) ** 2 for v in vals) ** 0.5
        dh  = sum((h - mh) ** 2 for h in hits) ** 0.5
        return num / (dx * dh) if dx * dh > 0 else 0

    hits   = [r[3] for r in rows]
    chgs   = [r[4] or 0 for r in rows]
    rsis   = [r[2] or 50 for r in rows]
    vms    = [r[5] or 0 for r in rows]
    vols   = [r[6] or 0 for r in rows]
    frs    = [r[7] or 0 for r in rows]
    holds  = [r[8] or 0 for r in rows]
    liqs   = [r[9] or 0 for r in rows]

    w = get_adaptive_weights()

    def adjust(w_cur, corr_val, lr=0.15):
        return round(max(0.2, min(2.0, w_cur + corr_val * lr)), 3)

    new_w = {
        "chg":     adjust(w["chg"],     corr(chgs,  hits)),
        "rsi":     adjust(w["rsi"],     corr(rsis,  hits)),
        "vm":      adjust(w["vm"],      corr(vms,   hits)),
        "vol":     adjust(w["vol"],     corr(vols,  hits)),
        "fr":      adjust(w["fr"],      corr(frs,   hits)),
        "holders": adjust(w["holders"], corr(holds, hits)),
        "liq":     adjust(w["liq"],     corr(liqs,  hits)),
    }

    acc = round(sum(hits) / len(hits) * 100, 1)

    db2 = get_db()
    db2.cursor().execute(
        """INSERT INTO score_weights
           (ts, w_chg, w_rsi, w_vm, w_vol, w_fr, w_holders, w_liq, accuracy)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            int(time.time()),
            new_w["chg"], new_w["rsi"], new_w["vm"],
            new_w["vol"], new_w["fr"], new_w["holders"], new_w["liq"],
            acc,
        ),
    )
    db2.commit()
    db2.close()

    log.info(f"Pesos adaptativos atualizados | acc={acc}% | {new_w}")
    return new_w


# ═══════════════════════════════════════
# SCORE PRINCIPAL
# ═══════════════════════════════════════

def calc_score(chg, rsi, vm, price, vol, liq, holders, fr=0.0, weights=None) -> int:
    """
    Score multi-fator de 10–99.
    Cada dimensão tem um peso adaptativo que evolui conforme acertos históricos.

    v2: baseline reduzido de 50→35 para exigir confluência real de fatores.
    Sinais mediocres que antes atingiam 60–70 agora ficam em 45–55.
    """
    if weights is None:
        weights = get_adaptive_weights()

    # ── Baseline mais conservador ─────────────────────────────────────────
    s = 40.0  # era 35 — baseline mais alto, diminui ruído de sinais marginais

    # Variação 24h
    w = weights.get("chg", 1.0)
    if chg > 20:    s += 15 * w
    elif chg > 10:  s += 10 * w
    elif chg > 5:   s +=  5 * w
    elif chg < -20: s -= 15 * w
    elif chg < -10: s -=  8 * w

    # RSI
    w = weights.get("rsi", 1.0)
    if rsi < 30:   s += 12 * w
    elif rsi < 40: s +=  7 * w
    elif rsi > 75: s -= 10 * w

    # Volume/MCap ratio
    w = weights.get("vm", 1.0)
    if vm > 50:   s += 10 * w
    elif vm > 20: s +=  5 * w

    # Volume absoluto
    w = weights.get("vol", 1.0)
    if vol > 1e6:   s += 8 * w
    elif vol > 1e5: s += 4 * w

    # Liquidez
    w = weights.get("liq", 1.0)
    if liq > 500_000:   s += 5 * w
    elif liq > 100_000: s += 2 * w

    # Holders
    w = weights.get("holders", 1.0)
    if holders > 10_000: s += 5 * w
    elif holders > 1_000: s += 2 * w

    # Preço baixo (upside assimétrico)
    if price < 0.001:  s += 8
    elif price < 0.01: s += 5
    elif price < 0.1:  s += 2

    # Funding Rate
    w = weights.get("fr", 1.0)
    if fr < -0.05:   s += 10 * w
    elif fr < -0.01: s +=  5 * w
    elif fr > 0.1:   s -=  8 * w
    elif fr > 0.05:  s -=  4 * w

    return max(10, min(99, round(s)))


# ═══════════════════════════════════════
# QUALITY GATE — entrada limpa ou nada
# ═══════════════════════════════════════

def passes_entry_quality(t: dict) -> tuple[bool, str]:
    """
    Filtro de qualidade de entrada antes de qualquer auto-trade.
    Exige confluência de fatores — não basta score alto isolado.

    Retorna (True, '') ou (False, motivo).

    Regras (TODAS devem passar):
      1. Score mínimo real ≥ 75
      2. RSI não sobrecomprado (< 78) — evita entrar no topo
      3. Volume mínimo para liquidez de saída
      4. BTC não em tendência baixista forte
      5. Se DUMP no contexto: bloquear long
      6. Pré-pump: exige confiança ≥ 0.55 (mais rigoroso que o alerta)
      7. Bônus: RSI real pesa +5 pts na exigência de score
    """
    score   = t.get("score", 0)
    rsi     = t.get("rsi", 50)
    vol     = t.get("vol", 0)
    chg     = t.get("chg", 0)
    rsi_real = t.get("rsi_real", False)
    pre      = t.get("pre", False)
    pre_conf = t.get("pre_conf", 0)

    # 1. Score mínimo — mais rigoroso se RSI é estimado
    #    Sinais de antecipação (pre/rev) aceitam 5pts a menos — entram antes do movimento
    signal_type = t.get("entry_signal", "")
    is_anticipation = any(s in signal_type.lower() for s in ("pre", "rev", "rsi", "reversa"))
    score_min = (70 if rsi_real else 75) if is_anticipation else (75 if rsi_real else 80)
    if score < score_min:
        return False, f"score_insuficiente:{score}<{score_min}"

    # 2. RSI: não entrar em ativo sobrecomprado
    if rsi >= 78:
        return False, f"rsi_sobrecomprado:{rsi}"

    # 3. Volume mínimo de liquidez
    if vol < 50_000:
        return False, f"volume_baixo:{vol:.0f}"

    # 4. Variação extrema negativa (possível dump em andamento)
    if chg <= -15:
        return False, f"dump_em_andamento:{chg:.1f}%"

    # 5. Pré-pump: exige confiança maior para trade (alerta é mais permissivo)
    if pre and pre_conf < 0.55:
        return False, f"pre_conf_baixa:{pre_conf:.2f}<0.55"

    # 6. BTC bear forte bloqueia entradas longas
    btc_mult = get_btc_context().get("score_mult", 1.0)
    if btc_mult <= 0.70:
        return False, f"btc_bear_forte:mult={btc_mult}"

    return True, ""


# ═══════════════════════════════════════
# PRIORIDADE DE ALERTA
# ═══════════════════════════════════════

def calc_alert_priority(t: dict, alert_type: str) -> int:
    """Calcula prioridade de 1–10 para um alerta."""
    p = 0

    if t["score"] >= 85:   p += 4
    elif t["score"] >= 75: p += 3
    elif t["score"] >= 65: p += 2
    elif t["score"] >= 55: p += 1

    if t["vm"] > 50:   p += 2
    elif t["vm"] > 25: p += 1

    if alert_type in ("PUMP", "PRÉ-PUMP", "GOLDEN CROSS") and t["rsi"] < 60:
        p += 1
    if alert_type == "RSI OVERSOLD" and t["rsi"] < 25:
        p += 2
    elif alert_type == "RSI OVERSOLD" and t["rsi"] < 30:
        p += 1

    fr = t.get("fr", 0)
    if alert_type in ("PUMP", "PRÉ-PUMP") and fr < -0.01:
        p += 1
    if alert_type == "REVERSÃO" and fr < -0.05:
        p += 2

    btc_mult = get_btc_context().get("score_mult", 1.0)
    if btc_mult >= 1.15:   p += 1
    elif btc_mult <= 0.75: p -= 2

    if t.get("rsi_real"):
        p += 1

    return max(1, min(10, p))
