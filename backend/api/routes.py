"""
api/routes.py — Rotas REST do SIREN PRO (PRODUÇÃO)

✔ Seguro (JWT + API keys protegidas)
✔ Estruturado pra backend real
✔ Sem endpoints públicos perigosos de IA
"""

import os
import time
import json
import logging
from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity

from ai.claude import analyze_text
from db.connection import get_db

log = logging.getLogger("SIREN.routes")
routes_bp = Blueprint("routes", __name__)

# Cache
_tokens_cache = {"ts": 0, "data": None}
_CACHE_TTL = 30


# =========================
# HELPERS
# =========================

def _db_stats():
    db = get_db()
    c = db.cursor()

    c.execute("SELECT COUNT(*), SUM(hit), AVG(pct_change) FROM alerts WHERE verified=1")
    row = c.fetchone()

    c.execute("SELECT COUNT(DISTINCT ts) FROM snapshots")
    snp = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM alerts")
    tot = c.fetchone()[0]

    db.close()

    t, h, avg = row[0] or 0, row[1] or 0, row[2] or 0

    return {
        "total_alerts": tot,
        "verified": t,
        "hits": int(h or 0),
        "rate_pct": round(h / t * 100) if t > 0 else 0,
        "avg_pct": round(avg or 0, 2),
        "snapshots": snp,
    }


# =========================
# ROTAS PÚBLICAS
# =========================

@routes_bp.route("/")
def home():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return send_file(os.path.join(base, "index.html"))


@routes_bp.route("/api/status")
def api_status():
    from core.scoring import get_adaptive_weights, get_btc_context

    stats = _db_stats()

    return jsonify({
        "version": "PRO",
        "alerts": stats,
        "snapshots": stats["snapshots"],
        "btc_context": get_btc_context(),
        "adaptive_weights": get_adaptive_weights(),
        "real_trading": os.environ.get("ENABLE_REAL_TRADING", "false"),
    })


@routes_bp.route("/api/tokens")
def api_tokens():
    global _tokens_cache

    now = time.time()
    limit = min(int(request.args.get("limit", 20)), 100)

    # Cache
    if _tokens_cache["data"] and (now - _tokens_cache["ts"]) < _CACHE_TTL:
        cached = _tokens_cache["data"]
        return jsonify({
            "tokens": cached["tokens"][:limit],
            "ts": cached["last_ts"],
            "count": len(cached["tokens"][:limit]),
            "cached": True,
        })

    db = get_db()
    c = db.cursor()

    c.execute("SELECT MAX(ts) FROM snapshots")
    last_ts = c.fetchone()[0]

    if not last_ts:
        db.close()
        return jsonify({"tokens": [], "ts": None})

    c.execute("""
        SELECT sym, price, chg, vol, mcap, liq, holders,
               rsi, ma9, ma21, score, tier
        FROM snapshots
        WHERE ts=%s
        ORDER BY score DESC
        LIMIT %s
    """, (last_ts, limit))

    rows = c.fetchall()
    db.close()

    cols = ["sym","price","chg","vol","mcap","liq","holders",
            "rsi","ma9","ma21","score","tier"]

    tokens = [dict(zip(cols, r)) for r in rows]

    _tokens_cache = {"ts": now, "data": {"tokens": tokens, "last_ts": last_ts}}

    return jsonify({
        "tokens": tokens,
        "ts": last_ts,
        "count": len(tokens)
    })


@routes_bp.route("/api/alerts")
def api_alerts():
    limit = min(int(request.args.get("limit", 20)), 100)
    sym = request.args.get("sym", "")

    db = get_db()
    c = db.cursor()

    if sym:
        c.execute("""
            SELECT ts, sym, price, label, score
            FROM alerts
            WHERE sym=%s
            ORDER BY ts DESC
            LIMIT %s
        """, (sym.upper(), limit))
    else:
        c.execute("""
            SELECT ts, sym, price, label, score
            FROM alerts
            ORDER BY ts DESC
            LIMIT %s
        """, (limit,))

    rows = c.fetchall()
    db.close()

    cols = ["ts","sym","price","label","score"]
    alerts = [dict(zip(cols, r)) for r in rows]

    return jsonify({"alerts": alerts})


# =========================
# IA (PROTEGIDO)
# =========================

@routes_bp.route("/api/ai/analyze-token", methods=["POST"])
@jwt_required()
def ai_analyze_token():
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        return jsonify({"error": "Claude não configurado"}), 503

    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")

    if not prompt:
        return jsonify({"error": "Prompt obrigatório"}), 400

    result = analyze_text(prompt)

    return jsonify({"text": result})


# =========================
# TRADES / POSIÇÕES
# =========================

@routes_bp.route("/api/positions")
def api_positions():
    try:
        db = get_db()
        c  = db.cursor()
        c.execute("""
            SELECT id, symbol, side, entry, size, stop, take_profit, status,
                   created_at, exit_price, mode, label, score
            FROM trades_v2
            ORDER BY created_at DESC
            LIMIT 200
        """)
        rows = c.fetchall()
        db.close()

        cols = ["id","symbol","side","entry","size","stop","take_profit",
                "status","created_at","exit_price","mode","label","score"]
        positions = [dict(zip(cols, r)) for r in rows]

        # Buscar preços atuais para posições abertas
        from trading.binance_client import get_price
        for p in positions:
            entry = float(p["entry"] or 0)
            size  = float(p["size"] or 0)
            if p["status"] == "OPEN" and entry > 0:
                try:
                    sym = (p["symbol"] or "").replace("USDT","").replace("FDUSD","")
                    current = get_price(f"{sym}USDT")
                    pnl_usdt = (current - entry) / entry * size
                    p["current_price"] = current
                    p["pnl_pct"]  = round((current - entry) / entry * 100, 2)
                    p["pnl"]      = round(pnl_usdt, 4)
                except Exception:
                    p["current_price"] = None
                    p["pnl_pct"]  = 0
                    p["pnl"]      = 0
            elif p["status"] == "CLOSED" and p["exit_price"] and entry > 0:
                exit_p = float(p["exit_price"])
                p["pnl"]     = round((exit_p - entry) / entry * size, 4)
                p["pnl_pct"] = round((exit_p - entry) / entry * 100, 2)
            else:
                p["pnl"] = 0
                p["pnl_pct"] = 0

        total     = len(positions)
        open_pos  = sum(1 for p in positions if p["status"] == "OPEN")
        total_pnl = sum(p["pnl"] for p in positions)

        return jsonify({
            "positions":  positions,
            "total":      total,
            "open":       open_pos,
            "total_pnl":  round(total_pnl, 4),
        })

    except Exception as e:
        log.error(f"api_positions erro: {e}")
        return jsonify({"error": str(e)}), 500



# =========================
# CONFIG
# =========================

@routes_bp.route("/config", methods=["POST"])
@jwt_required()
def set_config():
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}

    db = get_db()
    c = db.cursor()

    try:
        c.execute("""
            INSERT INTO configs (user_id, updated_at)
            VALUES (%s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET updated_at=%s
        """, (user_id, int(time.time()), int(time.time())))

        db.commit()
        db.close()

        return jsonify({"ok": True})

    except Exception as e:
        db.close()
        return jsonify({"error": str(e)}), 500
