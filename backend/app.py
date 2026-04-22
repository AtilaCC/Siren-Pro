"""
app.py — Ponto de entrada do SIREN PRO Backend.

Inicializa:
- Flask + CORS + JWT
- Banco de dados (PostgreSQL)
- Bot principal (thread assíncrona)
- API REST (Flask)
- WebSocket / SSE (Blueprint)
- Webhook Freqtrade (Blueprint) ← NOVO

Uso:
    python app.py
    gunicorn app:app -w 1 -k gevent --bind 0.0.0.0:3000
"""

import os
import asyncio
import logging
from threading import Thread
from flask import Flask, make_response, request
from flask_cors import CORS
from flask_jwt_extended import JWTManager

# ── Carrega .env se existir (desenvolvimento local) ───────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("SIREN")

# ── Imports internos (após dotenv) ────────────────────────────────────────
from db.models import init_db
from db.trades_db import init_trades_table
from api.auth import auth_bp
from api.routes import routes_bp
from realtime.ws import ws_bp
from api.freqtrade_webhook import freqtrade_bp   # ← NOVO
from core.engine import run_bot
from trading.position_monitor import run_monitor

# ═══════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════

app = Flask(__name__)

# JWT
app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY", "change_me_in_production")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = False

jwt = JWTManager(app)

# CORS
ALLOWED_ORIGINS = [
    "https://atilacc.github.io",
    "http://localhost",
    "http://localhost:3000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
]

CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

# ── Blueprints ────────────────────────────────────────────────────────────
app.register_blueprint(auth_bp)
app.register_blueprint(routes_bp)
app.register_blueprint(ws_bp)
app.register_blueprint(freqtrade_bp)   # ← NOVO

# ── CORS preflight handler ────────────────────────────────────────────────
@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return make_response("", 204)

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if any(origin.startswith(o) for o in ALLOWED_ORIGINS):
        response.headers["Access-Control-Allow-Origin"] = origin
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Max-Age"] = "3600"
    return response

# ═══════════════════════════════════════
# BOT THREAD
# ═══════════════════════════════════════

def _run_monitor_thread():
    try:
        asyncio.run(run_monitor())
    except Exception as e:
        log.error(f"Monitor encerrou com erro: {e}", exc_info=True)

def _run_bot_thread():
    try:
        asyncio.run(run_bot())
    except Exception as e:
        log.error(f"Bot encerrou com erro: {e}", exc_info=True)

# ═══════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════

def startup():
    log.info("=" * 55)
    log.info(" ⚡ SIREN PRO — Hedge Fund Engine")
    log.info("=" * 55)
    log.info(f" DB:        {'✅' if os.environ.get('DATABASE_URL') else '❌ DATABASE_URL ausente'}")
    log.info(f" Telegram:  {'✅' if os.environ.get('TG_TOKEN') else '⚠ não configurado'}")
    log.info(f" Claude AI: {'✅' if os.environ.get('ANTHROPIC_API_KEY') else '⚠ não configurado'}")
    log.info(f" Binance:   {'✅' if os.environ.get('BINANCE_API_KEY') else '⚠ não configurado'}")
    log.info(f" Freqtrade: {'✅ webhook ativo' if os.environ.get('FREQTRADE_WEBHOOK_SECRET') else '⚠ sem chave secreta'}")
    log.info(f" Real Trading: {'🔴 ATIVO' if os.environ.get('ENABLE_REAL_TRADING','false').lower()=='true' else '🟢 SIMULAÇÃO'}")
    log.info("=" * 55)

    init_db()
    init_trades_table()

    bot_thread = Thread(target=_run_bot_thread, daemon=True, name="SIREN-Bot")
    bot_thread.start()
    log.info("Bot iniciado em background thread")

    monitor_thread = Thread(target=_run_monitor_thread, daemon=True, name="SIREN-Monitor")
    monitor_thread.start()
    log.info("Monitor de posições iniciado em background thread")

startup()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    log.info(f"Flask rodando na porta {port}")
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
