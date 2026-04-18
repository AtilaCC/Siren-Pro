"""
trading/executor.py — Executor de sinais de trading.

Fluxo:
  1. Recebe sinal (token + label)
  2. Valida símbolo na Binance
  3. Verifica saldo
  4. Calcula posição via risk_manager
  5. Executa ordem (paper ou real)
  6. Persiste trade no banco
  7. Loga tudo

ENABLE_REAL_TRADING=false → modo paper (simulação)
ENABLE_REAL_TRADING=true  → ordens reais na Binance
"""

import os
import time
import logging

from trading.binance_client import (
    market_buy, market_sell, get_balance, validate_symbol, get_price,
)
from trading.risk_manager import calc_position_size, validate_trade
from db.connection import get_db

log = logging.getLogger("SIREN.executor")

ENABLE_REAL = os.environ.get("ENABLE_REAL_TRADING", "false").lower() == "true"


def execute_signal(token: dict, label: str, user_id: int = None) -> dict:
    """
    Executa um sinal de compra baseado em dados do token.

    Args:
        token:   dict com sym, price, score, etc.
        label:   tipo de sinal (PUMP, PRÉ-PUMP, etc.)
        user_id: ID do usuário (opcional, para registro)

    Returns:
        dict com resultado da execução
    """
    sym   = token["sym"]
    score = token["score"]
    price = token["price"]

    mode = "REAL" if ENABLE_REAL else "PAPER"
    log.info(f"[{mode}] Executando sinal {label} para ${sym} (score={score})")

    # ── 1. Validar símbolo ────────────────────────────────────────────────
    pair = f"{sym}USDT"
    if ENABLE_REAL and not validate_symbol(pair):
        log.warning(f"Símbolo {pair} inválido — abortando")
        return {"success": False, "error": f"Símbolo {pair} inválido ou não negociável"}

    # ── 2. Verificar saldo ────────────────────────────────────────────────
    balance = get_balance("USDT") if ENABLE_REAL else 1000.0  # saldo simulado no paper
    position = calc_position_size(balance, score, price)

    if position["usdt"] == 0:
        log.info(f"Posição zerada para ${sym} — ignorando trade")
        return {"success": False, "error": "Posição calculada abaixo do mínimo"}

    ok, reason = validate_trade(balance, position["usdt"])
    if not ok:
        log.warning(f"Validação de trade falhou: {reason}")
        return {"success": False, "error": reason}

    # ── 3. Executar ordem ─────────────────────────────────────────────────
    try:
        order = market_buy(pair, position["usdt"])
    except Exception as e:
        log.error(f"Erro ao executar ordem {pair}: {e}")
        return {"success": False, "error": str(e)}

    order_id = str(order.get("orderId", ""))
    exec_qty = float(order.get("executedQty", position["qty"]))
    total    = float(order.get("cummulativeQuoteQty", position["usdt"]))

    log.info(
        f"[{mode}] ✅ Ordem executada: {pair} | qty={exec_qty} | "
        f"total=${total:.2f} | order_id={order_id}"
    )

    # ── 4. Persistir no banco ─────────────────────────────────────────────
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
    )

    return {
        "success":    True,
        "mode":       mode,
        "symbol":     pair,
        "qty":        exec_qty,
        "price":      price,
        "total_usdt": total,
        "order_id":   order_id,
        "stop_price": position["stop_price"],
        "tp_price":   position["tp_price"],
        "risk_usdt":  position["risk_usdt"],
    }


def _save_trade(
    user_id, sym, side, qty, price, total_usdt, score, result, order_id
):
    """Persiste trade executado no banco de dados."""
    try:
        db = get_db()
        db.cursor().execute(
            """INSERT INTO trades
               (user_id, sym, side, qty, price, total_usdt, score, result, order_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (user_id, sym, side, qty, price, total_usdt, score, result, order_id, int(time.time())),
        )
        db.commit()
        db.close()
        log.info(f"Trade salvo: {side} ${sym} qty={qty} order={order_id}")
    except Exception as e:
        log.error(f"Erro ao salvar trade: {e}")
