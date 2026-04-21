"""
trading/executor.py — Executor de sinais de trading.

Fluxo OBRIGATÓRIO (nenhuma etapa pode ser pulada):
  1. Validar símbolo na Binance
  2. Verificar posição duplicada
  3. ✅ NOVO: Decisão da IA (claude_trade_decision) — confidence >= 0.75
  4. Verificar saldo
  5. Calcular posição via risk_manager (SL DINÂMICO — única fonte de verdade)
  6. Executar ordem (paper ou real)
  7. Persistir trade no banco

ENABLE_REAL_TRADING=false → modo paper (simulação)
ENABLE_REAL_TRADING=true  → ordens reais na Binance

v3: _calc_dynamic_sl_tp removido — SL/TP vêm exclusivamente do risk_manager.
    calc_position_size agora recebe o token completo para SL dinâmico.
"""

import os
import time
import asyncio
import logging

from trading.binance_client import (
    market_buy, get_balance, validate_symbol, get_price,
)
from trading.risk_manager import calc_position_size, validate_trade
from db.connection import get_db

log = logging.getLogger("SIREN.executor")

ENABLE_REAL = os.environ.get("ENABLE_REAL_TRADING", "false").lower() == "true"

# Confiança mínima da IA para permitir qualquer trade
AI_MIN_CONFIDENCE = float(os.environ.get("AI_MIN_CONFIDENCE", "0.75"))


# ══════════════════════════════════════════════
# EXECUTOR PRINCIPAL
# ══════════════════════════════════════════════

async def execute_signal_async(token: dict, label: str, user_id: int = None) -> dict:
    """
    Versão assíncrona do executor — usa IA para validar antes de executar.
    Chamada preferencial a partir do engine.py.
    """
    sym   = token["sym"]
    score = token["score"]
    price = token["price"]
    mode  = "REAL" if ENABLE_REAL else "PAPER"

    log.info(f"[{mode}] Iniciando pipeline para ${sym} | label={label} | score={score}")

    # ── 1. Validar símbolo ────────────────────────────────────────────────
    sym_clean = sym.encode("ascii", "ignore").decode("ascii").strip()
    if not sym_clean:
        return {"success": False, "error": f"Símbolo inválido: {sym}"}

    # ── 2. Verificar posição duplicada ────────────────────────────────────
    try:
        from trading.position_monitor import has_open_position
        if has_open_position(sym_clean) or has_open_position(sym):
            log.info(f"[{mode}] ${sym} já tem posição aberta — ignorando")
            return {"success": False, "error": "Posição já aberta"}
    except Exception:
        pass

    # ── 3. DECISÃO DA IA — ETAPA CRÍTICA ─────────────────────────────────
    try:
        from ai.claude_trade import claude_trade_decision
        ai_result = await claude_trade_decision(token)
    except Exception as e:
        log.error(f"Erro ao consultar IA para ${sym}: {e}")
        return {
            "success": False,
            "error":   f"Falha na consulta IA — trade bloqueado por segurança: {e}"
        }

    # Bloqueia se IA não aprovou
    if not ai_result.get("trade", False):
        log.info(
            f"[IA BLOQUEOU] ${sym} | conf={ai_result.get('confidence', 0):.2f} | "
            f"regime={ai_result.get('regime')} | {ai_result.get('reason')}"
        )
        return {
            "success":   False,
            "error":     f"IA bloqueou: {ai_result.get('reason')}",
            "ai_result": ai_result,
        }

    confidence = ai_result.get("confidence", 0)
    if confidence < AI_MIN_CONFIDENCE:
        log.info(f"[IA] ${sym} confidence {confidence:.2f} < {AI_MIN_CONFIDENCE} — bloqueado")
        return {
            "success":   False,
            "error":     f"Confidence {confidence:.2f} insuficiente (mín {AI_MIN_CONFIDENCE})",
            "ai_result": ai_result,
        }

    log.info(
        f"[IA APROVADO] ${sym} | conf={confidence:.2f} | "
        f"regime={ai_result.get('regime')} | {ai_result.get('reason')}"
    )

    # ── 4. Validar par na Binance ─────────────────────────────────────────
    pair = None
    for candidate in [f"{sym_clean}USDT", f"1000{sym_clean}USDT"]:
        if validate_symbol(candidate):
            pair = candidate
            break

    if pair is None:
        msg = f"Par {sym_clean}USDT não encontrado na Binance Spot"
        log.info(f"[{mode}] {msg}")
        return {"success": False, "error": msg}

    # ── 5. Verificar saldo e calcular posição (SL/TP via risk_manager) ────
    balance  = get_balance("USDT") if ENABLE_REAL else 1000.0
    position = calc_position_size(balance, score, price, token=token)

    if position["usdt"] == 0:
        return {"success": False, "error": "Posição calculada abaixo do mínimo"}

    ok, reason = validate_trade(balance, position["usdt"])
    if not ok:
        log.warning(f"Validação de trade falhou: {reason}")
        return {"success": False, "error": reason}

    # SL/TP vêm do risk_manager — não recalculamos aqui
    stop_price = position["stop_price"]
    tp_price   = position["tp_price"]
    sl_pct     = position["sl_pct"]

    log.info(f"[{mode}] ${sym} | SL={sl_pct*100:.1f}% (${stop_price:.8f}) | TP=${tp_price:.8f} | tier={position['tier']} | R/R={position['rr']:.1f}")

    # ── 6. Executar ordem ─────────────────────────────────────────────────
    if not ENABLE_REAL:
        order = {
            "orderId":             f"PAPER_{int(time.time())}_{sym_clean}",
            "executedQty":         str(position["qty"]),
            "cummulativeQuoteQty": str(position["usdt"]),
            "status":              "FILLED",
        }
        log.info(f"[PAPER] Ordem simulada: {pair} qty={position['qty']:.4f} total=${position['usdt']:.2f}")
    else:
        try:
            order = market_buy(pair, position["usdt"])
        except Exception as e:
            log.error(f"Erro ao executar ordem {pair}: {e}")
            return {"success": False, "error": str(e)}

    order_id = str(order.get("orderId", ""))
    exec_qty = float(order.get("executedQty", position["qty"]))
    total    = float(order.get("cummulativeQuoteQty", position["usdt"]))

    log.info(
        f"[{mode}] ✅ ${sym} executado | qty={exec_qty} | "
        f"total=${total:.2f} | SL=${stop_price:.8f} | TP=${tp_price:.8f}"
    )

    # ── 7. Persistir no banco ─────────────────────────────────────────────
    _save_trade(
        user_id=user_id,
        sym=sym,
        side="BUY",
        qty=exec_qty,
        price=price,
        total_usdt=total,
        score=score,
        result=mode,
        order_id=order_id,
        stop_price=stop_price,
        tp_price=tp_price,
        ai_confidence=confidence,
        ai_regime=ai_result.get("regime", "?"),
        label=label,
    )

    return {
        "success":      True,
        "mode":         mode,
        "symbol":       pair,
        "qty":          exec_qty,
        "price":        price,
        "total_usdt":   total,
        "order_id":     order_id,
        "stop_price":   stop_price,
        "tp_price":     tp_price,
        "sl_pct":       sl_pct,
        "tier":         position["tier"],
        "rr":           position["rr"],
        "risk_usdt":    position.get("risk_usdt", 0),
        "ai_confidence": confidence,
        "ai_regime":    ai_result.get("regime"),
        "ai_reason":    ai_result.get("reason"),
    }


def execute_signal(token: dict, label: str, user_id: int = None) -> dict:
    """
    Wrapper síncrono para compatibilidade com código não-async do engine.
    Internamente chama execute_signal_async via event loop.
    """
    try:
        loop = asyncio.get_running_loop()
        # Se já há um loop rodando (dentro de contexto async), agenda como task
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(
                asyncio.run,
                execute_signal_async(token, label, user_id)
            )
            return future.result(timeout=30)
    except RuntimeError:
        # Não há loop rodando — cria um novo
        return asyncio.run(execute_signal_async(token, label, user_id))
    except Exception as e:
        log.error(f"execute_signal wrapper erro: {e}")
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════
# PERSISTÊNCIA
# ══════════════════════════════════════════════

def _save_trade(
    user_id, sym, side, qty, price, total_usdt,
    score, result, order_id,
    stop_price, tp_price,
    ai_confidence=0.0, ai_regime="?", label="?"
):
    """Persiste trade executado no banco de dados."""
    try:
        db = get_db()
        c  = db.cursor()

        try:
            c.execute(
                """INSERT INTO trades_v2
                   (symbol, side, qty, entry, size, stop, take_profit,
                    score, mode, order_id, label, status, created_at,
                    ai_confidence, ai_regime)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s,%s,%s)""",
                (
                    sym, side, qty, price, total_usdt,
                    stop_price, tp_price,
                    score, result, order_id, label,
                    int(time.time()),
                    round(ai_confidence, 4), ai_regime,
                ),
            )
        except Exception:
            # Fallback sem colunas AI (schema antigo)
            db.rollback()
            c.execute(
                """INSERT INTO trades_v2
                   (symbol, side, qty, entry, size, stop, take_profit,
                    score, mode, order_id, label, status, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)""",
                (
                    sym, side, qty, price, total_usdt,
                    stop_price, tp_price,
                    score, result, order_id, label,
                    int(time.time()),
                ),
            )

        # Tabela legada para compatibilidade
        try:
            c.execute(
                """INSERT INTO trades
                   (user_id, sym, side, qty, price, total_usdt, score, result, order_id, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
                (user_id, sym, side, qty, price, total_usdt, score, result, order_id, int(time.time())),
            )
        except Exception:
            pass

        db.commit()
        db.close()
        log.info(
            f"Trade salvo: {side} ${sym} qty={qty} | "
            f"SL=${stop_price:.8f} TP=${tp_price:.8f} | "
            f"AI conf={ai_confidence:.2f} regime={ai_regime}"
        )
    except Exception as e:
        log.error(f"Erro ao salvar trade: {e}")
