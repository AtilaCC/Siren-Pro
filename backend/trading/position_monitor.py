"""
trading/position_monitor.py — Monitor de posições abertas.

Roda em loop assíncrono separado (thread daemon), sem travar o engine principal.

Fluxo por posição:
  1. Busca preço atual na Binance (ou simula em paper)
  2. Aplica trailing stop (atualiza stop se preço subiu)
  3. Se preço <= stop    → fecha por STOP LOSS
  4. Se preço >= tp      → fecha por TAKE PROFIT
  5. Atualiza banco via update_trade_db()

Parâmetros configuráveis via env:
  MONITOR_INTERVAL_SECONDS  — intervalo do loop (padrão: 15s)
  TRAILING_STOP_PCT         — percentual de trailing (padrão: 2%)
"""

import os
import asyncio
import logging
import time

from db.connection import get_db
from db.trades_db import update_trade_db

log = logging.getLogger("SIREN.monitor")

MONITOR_INTERVAL  = int(os.environ.get("MONITOR_INTERVAL_SECONDS", 15))
TRAILING_STOP_PCT = float(os.environ.get("TRAILING_STOP_PCT", 0.02))  # 2%
ENABLE_REAL       = os.environ.get("ENABLE_REAL_TRADING", "false").lower() == "true"


# ═══════════════════════════════════════
# PREÇO ATUAL
# ═══════════════════════════════════════

def _get_current_price(symbol: str) -> float:
    """
    Busca preço atual do par SYMBOLUSDT.
    Tenta Binance Spot primeiro, depois Alpha API como fallback
    (tokens Alpha não existem na Spot regular).
    Retorna 0.0 em caso de falha.
    """
    import requests

    sym_clean = symbol.encode("ascii", "ignore").decode("ascii").strip()
    if not sym_clean:
        return 0.0

    # Tenta variantes: XUSDT, 1000XUSDT
    for pair in [f"{sym_clean}USDT", f"1000{sym_clean}USDT"]:
        try:
            from trading.binance_client import get_price, validate_symbol, SYMBOLS_CACHE
            if SYMBOLS_CACHE and not validate_symbol(pair):
                continue
            price = get_price(pair)
            if price > 0:
                return price
        except Exception:
            pass

    # Fallback: API Alpha da Binance
    try:
        url = "https://www.binance.com/bapi/asset/v1/public/asset/asset/get-all-asset"
        # Tenta endpoint de preço do Alpha
        alpha_url = f"https://www.binance.com/bapi/bigdata/v1/public/bigdata/finance/exchange/listByProductId?productId=BINANCE_ALPHA&pageIndex=1&pageSize=200"
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={sym_clean}USDT",
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            price = float(data.get("price", 0))
            if price > 0:
                return price
    except Exception:
        pass

    log.warning(f"_get_current_price ${symbol}: símbolo não encontrado na Spot nem Alpha — pulando")
    return 0.0


# ═══════════════════════════════════════
# BUSCAR POSIÇÕES ABERTAS
# ═══════════════════════════════════════

def has_open_position(symbol: str) -> bool:
    """
    Verifica se já existe posição OPEN para o símbolo.
    Usado pelo executor para evitar entradas duplicadas.
    """
    try:
        db = get_db()
        c  = db.cursor()
        c.execute(
            "SELECT COUNT(*) FROM trades_v2 WHERE symbol=%s AND status='OPEN'",
            (symbol,)
        )
        count = c.fetchone()[0]
        db.close()
        return count > 0
    except Exception as e:
        log.error(f"has_open_position ${symbol}: {e}")
        return False


def _fetch_open_positions() -> list:
    """
    Retorna todas as posições com status=OPEN da tabela trades_v2.
    """
    try:
        db = get_db()
        c  = db.cursor()
        c.execute(
            """SELECT id, symbol, entry, size, stop, take_profit
               FROM trades_v2
               WHERE status = 'OPEN'
               ORDER BY created_at ASC"""
        )
        rows = c.fetchall()
        db.close()
        return [
            {
                "id":          r[0],
                "symbol":      r[1],
                "entry":       float(r[2]),
                "size":        float(r[3]),
                "stop":        float(r[4]),
                "take_profit": float(r[5]),
            }
            for r in rows
        ]
    except Exception as e:
        log.error(f"_fetch_open_positions falhou: {e}")
        return []


# ═══════════════════════════════════════
# TRAILING STOP
# ═══════════════════════════════════════

def _apply_trailing_stop(position: dict, current_price: float) -> float:
    """
    Calcula novo stop com trailing.
    Se o preço subiu acima do stop atual + trailing%, sobe o stop.
    Nunca deixa o stop cair abaixo do valor anterior.

    Retorna o novo valor de stop (pode ser igual ao anterior).
    """
    trailing_stop = round(current_price * (1 - TRAILING_STOP_PCT), 8)
    if trailing_stop > position["stop"]:
        return trailing_stop
    return position["stop"]


def _update_stop_in_db(trade_id: int, new_stop: float):
    """Persiste o novo stop no banco sem fechar o trade."""
    try:
        db = get_db()
        db.cursor().execute(
            "UPDATE trades_v2 SET stop=%s WHERE id=%s",
            (new_stop, trade_id),
        )
        db.commit()
        db.close()
    except Exception as e:
        log.error(f"_update_stop_in_db id={trade_id}: {e}")


# ═══════════════════════════════════════
# FECHAR POSIÇÃO
# ═══════════════════════════════════════

def _close_position(position: dict, exit_price: float, reason: str):
    """
    Fecha a posição: executa venda (real ou paper) e atualiza banco.
    """
    sym = position["symbol"]
    log.info(
        f"[MONITOR] Fechando ${sym} por {reason} | "
        f"entry={position['entry']} exit={exit_price} "
        f"stop={position['stop']} tp={position['take_profit']}"
    )

    # Executa venda apenas em modo real
    if ENABLE_REAL:
        try:
            from trading.binance_client import market_sell
            qty = round(position["size"] / position["entry"], 6)
            market_sell(f"{sym}USDT", qty)
        except Exception as e:
            log.error(f"[MONITOR] market_sell ${sym} falhou: {e}")
            # Mesmo com erro na venda, fecha no banco para não ficar preso
    else:
        pnl_pct = (exit_price - position["entry"]) / position["entry"] * 100
        log.info(
            f"[PAPER] 🧾 Fechando ${sym} | "
            f"exit={exit_price} | "
            f"pnl={pnl_pct:+.2f}% | motivo={reason}"
        )

    # Atualiza banco
    update_trade_db(position, exit_price)
    log.info(f"[MONITOR] ${sym} CLOSED ({reason})")


# ═══════════════════════════════════════
# CICLO DE MONITORAMENTO
# ═══════════════════════════════════════

async def monitor_cycle():
    """
    Um ciclo de verificação de todas as posições abertas.
    """
    positions = _fetch_open_positions()
    if not positions:
        return

    log.info(f"[MONITOR] Verificando {len(positions)} posição(ões) aberta(s)")

    for pos in positions:
        sym           = pos["symbol"]
        current_price = _get_current_price(sym)

        if current_price <= 0:
            log.warning(f"[MONITOR] Preço inválido para ${sym} — pulando")
            continue

        # Trailing stop — atualiza antes de checar fechamento
        new_stop = _apply_trailing_stop(pos, current_price)
        if new_stop > pos["stop"]:
            log.info(
                f"[MONITOR] Trailing stop ${sym}: "
                f"{pos['stop']:.8f} → {new_stop:.8f} "
                f"(preço atual={current_price:.8f})"
            )
            _update_stop_in_db(pos["id"], new_stop)
            pos["stop"] = new_stop  # atualiza local para checar abaixo

        # Stop loss atingido
        if current_price <= pos["stop"]:
            _close_position(pos, current_price, "STOP_LOSS")
            continue

        # Take profit atingido
        if current_price >= pos["take_profit"]:
            _close_position(pos, current_price, "TAKE_PROFIT")
            continue

        # Posição ainda aberta — loga status
        pnl_pct = (current_price - pos["entry"]) / pos["entry"] * 100
        log.info(
            f"[MONITOR] ${sym} OPEN | "
            f"entry={pos['entry']:.8f} now={current_price:.8f} "
            f"pnl={pnl_pct:+.2f}% | "
            f"SL={pos['stop']:.8f} TP={pos['take_profit']:.8f}"
        )

        await asyncio.sleep(0.2)  # throttle entre tokens


# ═══════════════════════════════════════
# LOOP PRINCIPAL
# ═══════════════════════════════════════

async def run_monitor():
    """
    Loop infinito do monitor de posições.
    Roda em asyncio separado, não bloqueia o engine principal.
    """
    log.info(f"[MONITOR] Iniciado | intervalo={MONITOR_INTERVAL}s | trailing={TRAILING_STOP_PCT*100:.1f}%")
    while True:
        try:
            await monitor_cycle()
        except Exception as e:
            log.error(f"[MONITOR] Erro no ciclo: {e}", exc_info=True)
        await asyncio.sleep(MONITOR_INTERVAL)
