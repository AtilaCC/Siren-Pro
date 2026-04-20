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
    # Limpa o símbolo: remove caracteres não-ASCII (tokens chineses, etc.)
    sym_clean = sym.encode("ascii", "ignore").decode("ascii").strip()
    if not sym_clean:
        log.warning(f"Símbolo ${sym} contém apenas caracteres não-ASCII — ignorando")
        return {"success": False, "error": f"Símbolo inválido para trading: {sym}"}

    # Tenta variantes do par: XUSDT, 1000XUSDT
    pair = None
    for candidate in [f"{sym_clean}USDT", f"1000{sym_clean}USDT"]:
        if validate_symbol(candidate):
            pair = candidate
            break

    if pair is None:
        if ENABLE_REAL:
            log.warning(f"Par {sym_clean}USDT não encontrado na Binance — ignorando")
            return {"success": False, "error": f"Par {sym_clean}USDT não negociável na Binance Spot"}
        else:
            # PAPER: aceita mesmo sem validação (token pode existir mas API retornou erro)
            pair = f"{sym_clean}USDT"
            log.info(f"[PAPER] Par {pair} não validado pela API — aceitando para simulação")

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
    if not ENABLE_REAL:
        # PAPER: simula ordem sem chamar Binance
        order = {
            "orderId":               f"PAPER_{int(time.time())}_{sym_clean}",
            "executedQty":           str(position["qty"]),
            "cummulativeQuoteQty":   str(position["usdt"]),
            "status":                "FILLED",
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
        db  = get_db()
        c   = db.cursor()
        # Calcula stop e TP a partir do price
        stop_price = round(price * 0.95, 8)   # 5% stop loss
        tp_price   = round(price * 1.10, 8)   # 10% take profit

        # Salva em trades_v2 (lida pelo position_monitor e pela API /api/positions)
        c.execute(
            """INSERT INTO trades_v2
               (user_id, symbol, side, qty, entry, size, stop, take_profit,
                score, mode, order_id, label, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s)""",
            (user_id, sym, side, qty, price, total_usdt, stop_price, tp_price,
             score, result, order_id, result, int(time.time())),
        )
        # Mantém também a tabela antiga para compatibilidade
        c.execute(
            """INSERT INTO trades
               (user_id, sym, side, qty, price, total_usdt, score, result, order_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (user_id, sym, side, qty, price, total_usdt, score, result, order_id, int(time.time())),
        )
        db.commit()
        db.close()
        log.info(f"Trade salvo em trades_v2: {side} ${sym} qty={qty} SL={stop_price} TP={tp_price}")
    except Exception as e:
        log.error(f"Erro ao salvar trade: {e}")
