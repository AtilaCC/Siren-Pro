"""
db/trades_db.py — Persistência de trades com status OPEN/CLOSED.

Tabela: trades_v2
  Separada da tabela 'trades' original para não alterar estrutura existente.

Funções públicas:
  init_trades_table()        — chamada pelo app.py no startup
  save_trade_db(trade)       — registra trade novo como OPEN
  update_trade_db(trade, exit_price) — fecha trade e calcula PnL
"""

import time
import logging
from db.connection import get_db

log = logging.getLogger("SIREN.trades_db")


# ═══════════════════════════════════════
# INICIALIZAÇÃO DA TABELA
# ═══════════════════════════════════════

def init_trades_table():
    """
    Cria a tabela trades_v2 se não existir.
    Chamada no startup do app — segura para rodar múltiplas vezes (IF NOT EXISTS).
    Não altera nenhuma tabela existente.
    """
    db = get_db()
    c  = db.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS trades_v2 (
        id           SERIAL PRIMARY KEY,
        symbol       TEXT    NOT NULL,
        entry        REAL    NOT NULL,
        size         REAL    NOT NULL,
        stop         REAL    NOT NULL,
        take_profit  REAL    NOT NULL,
        status       TEXT    NOT NULL DEFAULT 'OPEN',   -- OPEN | CLOSED
        pnl          REAL    DEFAULT 0,
        exit_price   REAL    DEFAULT 0,
        label        TEXT,
        score        INTEGER DEFAULT 0,
        mode         TEXT    DEFAULT 'PAPER',            -- PAPER | REAL
        created_at   BIGINT  NOT NULL,
        closed_at    BIGINT  DEFAULT 0
    )""")

    c.execute("CREATE INDEX IF NOT EXISTS idx_trades_v2_sym    ON trades_v2(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trades_v2_status ON trades_v2(status)")

    db.commit()
    db.close()
    log.info("Tabela trades_v2 inicializada")


# ═══════════════════════════════════════
# SAVE
# ═══════════════════════════════════════

def save_trade_db(trade: dict) -> int | None:
    """
    Registra um novo trade como OPEN no banco.

    Campos esperados no dict trade:
      symbol      : str   — ex: "PEPE"
      entry       : float — preço de entrada
      size        : float — valor em USDT
      stop        : float — preço de stop loss
      take_profit : float — preço de take profit
      label       : str   — tipo de sinal (opcional)
      score       : int   — score SIREN (opcional)
      mode        : str   — "PAPER" ou "REAL" (opcional, padrão PAPER)

    Retorna o ID do trade inserido, ou None em caso de erro.
    """
    required = {"symbol", "entry", "size", "stop", "take_profit"}
    missing  = required - trade.keys()
    if missing:
        log.error(f"save_trade_db: campos obrigatórios ausentes: {missing}")
        return None

    try:
        db = get_db()
        c  = db.cursor()
        c.execute(
            """INSERT INTO trades_v2
               (symbol, entry, size, stop, take_profit, status, pnl,
                label, score, mode, created_at)
               VALUES (%s, %s, %s, %s, %s, 'OPEN', 0, %s, %s, %s, %s)
               RETURNING id""",
            (
                trade["symbol"],
                float(trade["entry"]),
                float(trade["size"]),
                float(trade["stop"]),
                float(trade["take_profit"]),
                trade.get("label", ""),
                int(trade.get("score", 0)),
                trade.get("mode", "PAPER"),
                int(time.time()),
            ),
        )
        trade_id = c.fetchone()[0]
        db.commit()
        db.close()
        log.info(
            f"save_trade_db: ${trade['symbol']} OPEN | "
            f"entry={trade['entry']} size={trade['size']} "
            f"SL={trade['stop']} TP={trade['take_profit']} id={trade_id}"
        )
        return trade_id
    except Exception as e:
        log.error(f"save_trade_db falhou: {e}")
        return None


# ═══════════════════════════════════════
# UPDATE (fechar trade)
# ═══════════════════════════════════════

def update_trade_db(trade: dict, exit_price: float) -> bool:
    """
    Fecha um trade existente, calcula PnL e marca como CLOSED.

    Args:
      trade       : dict com campo 'id' (retornado pelo save_trade_db)
      exit_price  : float — preço de saída

    PnL = (exit_price - entry) / entry * size  (em USDT)
    Retorna True se atualizado com sucesso, False em caso de erro.
    """
    trade_id = trade.get("id")
    if not trade_id:
        log.error("update_trade_db: campo 'id' ausente no trade")
        return False

    if exit_price <= 0:
        log.error(f"update_trade_db: exit_price inválido ({exit_price})")
        return False

    try:
        db = get_db()
        c  = db.cursor()

        # Busca entry e size para calcular PnL
        c.execute("SELECT entry, size FROM trades_v2 WHERE id=%s", (trade_id,))
        row = c.fetchone()
        if not row:
            log.error(f"update_trade_db: trade id={trade_id} não encontrado")
            db.close()
            return False

        entry, size = row
        pnl = round((exit_price - entry) / entry * size, 4) if entry > 0 else 0

        c.execute(
            """UPDATE trades_v2
               SET status='CLOSED', exit_price=%s, pnl=%s, closed_at=%s
               WHERE id=%s""",
            (float(exit_price), pnl, int(time.time()), trade_id),
        )
        db.commit()
        db.close()

        result = "✅ LUCRO" if pnl >= 0 else "❌ PERDA"
        log.info(
            f"update_trade_db: id={trade_id} CLOSED | "
            f"exit={exit_price} pnl={pnl:+.4f} USDT {result}"
        )
        return True
    except Exception as e:
        log.error(f"update_trade_db falhou: {e}")
        return False
