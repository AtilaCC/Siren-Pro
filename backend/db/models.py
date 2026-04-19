"""
db/models.py — Inicialização e criação de todas as tabelas do sistema.

Tabelas originais do SIREN v6:
  snapshots, alerts, spam, score_weights, backtest_results, claude_analyses

Tabelas novas (autenticação e trading):
  users, configs, trades
"""

import time
import logging
from db.connection import get_db

log = logging.getLogger("SIREN.db.models")


def init_db():
    """Cria todas as tabelas e índices se não existirem."""
    db = get_db()
    c  = db.cursor()

    # ── Tabelas originais SIREN v6 ─────────────────────────────────────────

    c.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        id       SERIAL PRIMARY KEY,
        ts       BIGINT NOT NULL,
        sym      TEXT NOT NULL,
        price    REAL, chg REAL, vol REAL, mcap REAL, liq REAL,
        holders  INTEGER,
        rsi      REAL,  rsi_real  INTEGER DEFAULT 0,
        ma9      REAL,  ma21      REAL,   gc INTEGER DEFAULT 0,
        score    INTEGER, tier TEXT,
        funding_rate  REAL,  fr_real INTEGER DEFAULT 0,
        chain    TEXT,  vm REAL,
        vol_growth          REAL DEFAULT 0,
        price_compression   REAL DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id          SERIAL PRIMARY KEY,
        ts          BIGINT NOT NULL,
        sym         TEXT NOT NULL,
        price       REAL,
        label       TEXT,
        score       INTEGER,
        rsi         REAL,
        priority    INTEGER DEFAULT 0,
        verified    INTEGER DEFAULT 0,
        verified_at BIGINT,
        exit_price  REAL,
        pct_change  REAL,
        hit         INTEGER,
        max_gain    REAL DEFAULT 0,
        max_drawdown    REAL DEFAULT 0,
        bars_to_target  INTEGER DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS spam (
        key TEXT PRIMARY KEY,
        ts  BIGINT NOT NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS score_weights (
        id        SERIAL PRIMARY KEY,
        ts        BIGINT NOT NULL,
        w_chg     REAL DEFAULT 1.0,
        w_rsi     REAL DEFAULT 1.0,
        w_vm      REAL DEFAULT 1.0,
        w_vol     REAL DEFAULT 1.0,
        w_fr      REAL DEFAULT 1.0,
        w_holders REAL DEFAULT 1.0,
        w_liq     REAL DEFAULT 1.0,
        accuracy  REAL DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS backtest_results (
        id                  SERIAL PRIMARY KEY,
        ts                  BIGINT NOT NULL,
        label               TEXT,
        n_trades            INTEGER,
        win_rate            REAL,
        avg_return          REAL,
        avg_max_gain        REAL,
        avg_drawdown        REAL,
        avg_bars_to_target  REAL,
        profit_factor       REAL,
        sharpe              REAL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS claude_analyses (
        id            SERIAL PRIMARY KEY,
        ts            BIGINT NOT NULL,
        sym           TEXT,
        analysis_type TEXT,
        verdict       TEXT,
        reasoning     TEXT,
        raw_response  TEXT
    )""")

    # ── Novas tabelas: autenticação + configuração + trades ───────────────

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id         SERIAL PRIMARY KEY,
        username   TEXT UNIQUE NOT NULL,
        password   TEXT NOT NULL,
        created_at BIGINT NOT NULL DEFAULT EXTRACT(epoch FROM NOW())
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS configs (
        id             SERIAL PRIMARY KEY,
        user_id        INTEGER REFERENCES users(id) ON DELETE CASCADE,
        telegram_token TEXT,
        telegram_chat  TEXT,
        claude_key     TEXT,
        binance_key    TEXT,
        binance_secret TEXT,
        real_trading   BOOLEAN DEFAULT FALSE,
        updated_at     BIGINT NOT NULL DEFAULT EXTRACT(epoch FROM NOW()),
        UNIQUE(user_id)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id         SERIAL PRIMARY KEY,
        user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
        sym        TEXT NOT NULL,
        side       TEXT NOT NULL,          -- BUY / SELL
        qty        REAL,
        price      REAL,
        total_usdt REAL,
        score      INTEGER,
        result     TEXT,                   -- PAPER / REAL
        order_id   TEXT,
        created_at BIGINT NOT NULL DEFAULT EXTRACT(epoch FROM NOW())
    )""")

    # ── Índices ────────────────────────────────────────────────────────────
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap_sym    ON snapshots(sym)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap_ts     ON snapshots(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap_sym_ts ON snapshots(sym, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alert_v     ON alerts(verified)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trades_uid  ON trades(user_id)")

    # ── Pesos padrão se ainda não existir ─────────────────────────────────
    c.execute("SELECT 1 FROM score_weights LIMIT 1")
    if not c.fetchone():
        c.execute(
            """INSERT INTO score_weights
               (ts, w_chg, w_rsi, w_vm, w_vol, w_fr, w_holders, w_liq, accuracy)
               VALUES (%s, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0)""",
            (int(time.time()),),
        )

    db.commit()
    db.close()
    log.info("Database SIREN PRO inicializado")
