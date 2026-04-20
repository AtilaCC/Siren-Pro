"""
api/routes.py — Rotas REST do SIREN PRO.

Endpoints públicos (sem auth):
  GET  /                  → dashboard HTML
  GET  /api/status        → status geral
  GET  /api/tokens        → top tokens do último snapshot
  GET  /api/alerts        → alertas com filtros
  GET  /api/backtest      → resultados de backtest
  GET  /api/narrative     → narrativa de mercado detectada
  GET  /api/weights       → histórico de pesos adaptativos
  GET  /api/summary       → sumário completo

Endpoints autenticados (JWT):
  POST /analyze           → análise Claude de texto livre
  GET  /history           → histórico de trades do usuário
  POST /config            → salva configurações do usuário
  GET  /config            → lê configurações do usuário
"""

import os
import time
import json
import logging
from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity

from ai.claude import analyze_text
from db.connection import get_db

log      = logging.getLogger("SIREN.routes")
routes_bp = Blueprint("routes", __name__)

# Cache em memória para /api/tokens
_tokens_cache: dict = {"ts": 0, "data": None}
_CACHE_TTL = 30


# ═══════════════════════════════════════
# DASHBOARD HTML (mantido do v6)
# ═══════════════════════════════════════



# ═══════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════

def _db_stats() -> dict:
    db = get_db()
    c  = db.cursor()
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
        "verified":     t,
        "hits":         int(h or 0),
        "rate_pct":     round(h / t * 100) if t > 0 else 0,
        "avg_pct":      round(avg or 0, 2),
        "snapshots":    snp,
    }


# ═══════════════════════════════════════
# ROTAS PÚBLICAS
# ═══════════════════════════════════════

@routes_bp.route("/")
def home():
    import os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return send_file(os.path.join(base, "index.html"))


@routes_bp.route("/api/status")
def api_status():
    from core.scoring import get_adaptive_weights, get_btc_context
    stats = _db_stats()
    db    = get_db()
    c     = db.cursor()
    c.execute(
        "SELECT label, win_rate, profit_factor, sharpe, n_trades FROM backtest_results ORDER BY ts DESC LIMIT 4"
    )
    bt = c.fetchall()
    db.close()
    return jsonify({
        "version":          "PRO",
        "alerts":           stats,
        "snapshots":        stats["snapshots"],
        "btc_context":      get_btc_context(),
        "adaptive_weights": get_adaptive_weights(),
        "real_trading":     __import__("os").environ.get("ENABLE_REAL_TRADING", "false"),
        "backtest_latest":  [
            {"label": r[0], "win_rate": r[1], "profit_factor": r[2], "sharpe": r[3], "n_trades": r[4]}
            for r in bt
        ],
    })


@routes_bp.route("/api/tokens")
def api_tokens():
    global _tokens_cache
    now   = time.time()
    limit = min(int(request.args.get("limit", 20)), 100)

    if _tokens_cache["data"] and (now - _tokens_cache["ts"]) < _CACHE_TTL:
        cached = _tokens_cache["data"]
        return jsonify({
            "tokens": cached["tokens"][:limit],
            "ts":     cached["last_ts"],
            "count":  len(cached["tokens"][:limit]),
            "cached": True,
        })

    db = get_db()
    c  = db.cursor()
    c.execute("SELECT MAX(ts) FROM snapshots")
    last_ts = c.fetchone()[0]
    if not last_ts:
        db.close()
        return jsonify({"tokens": [], "ts": None})

    c.execute(
        """SELECT sym, price, chg, vol, mcap, liq, holders, rsi, rsi_real,
                  ma9, ma21, gc, score, tier, funding_rate, fr_real, chain, vm,
                  vol_growth, price_compression
           FROM snapshots WHERE ts=%s ORDER BY score DESC LIMIT %s""",
        (last_ts, limit),
    )
    rows = c.fetchall()
    db.close()

    cols   = ["sym","price","chg","vol","mcap","liq","holders","rsi","rsi_real",
              "ma9","ma21","gc","score","tier","fr","fr_real","chain","vm",
              "vol_growth","price_compression"]
    tokens = [dict(zip(cols, r)) for r in rows]
    _tokens_cache = {"ts": now, "data": {"tokens": tokens, "last_ts": last_ts}}
    return jsonify({"tokens": tokens[:limit], "ts": last_ts, "count": len(tokens[:limit])})


@routes_bp.route("/api/alerts")
def api_alerts():
    limit    = min(int(request.args.get("limit", 20)), 100)
    verified = request.args.get("verified")
    sym      = request.args.get("sym", "")
    db       = get_db()
    c        = db.cursor()

    q  = "SELECT ts, sym, price, label, score, rsi, priority, verified, pct_change, hit FROM alerts"
    wh, pa = [], []
    if verified is not None:
        wh.append("verified=%s"); pa.append(int(verified))
    if sym:
        wh.append("sym=%s"); pa.append(sym.upper())
    if wh:
        q += " WHERE " + " AND ".join(wh)
    q += " ORDER BY ts DESC LIMIT %s"
    pa.append(limit)

    c.execute(q, pa)
    rows = c.fetchall()
    db.close()

    cols   = ["ts","sym","price","label","score","rsi","priority","verified","pct","hit"]
    alerts = [dict(zip(cols, r)) for r in rows]
    return jsonify({"alerts": alerts, "count": len(alerts)})


@routes_bp.route("/api/backtest")
def api_backtest():
    db = get_db()
    c  = db.cursor()
    c.execute(
        """SELECT ts, label, n_trades, win_rate, avg_return, avg_max_gain,
                  avg_drawdown, avg_bars_to_target, profit_factor, sharpe
           FROM backtest_results ORDER BY ts DESC LIMIT 10"""
    )
    rows = c.fetchall()
    db.close()
    cols = ["ts","label","n_trades","win_rate","avg_return","avg_max_gain",
            "avg_drawdown","avg_bars_to_target","profit_factor","sharpe"]
    return jsonify({"results": [dict(zip(cols, r)) for r in rows]})


@routes_bp.route("/api/narrative")
def api_narrative():
    db = get_db()
    c  = db.cursor()
    c.execute(
        """SELECT ts, reasoning, raw_response FROM claude_analyses
           WHERE analysis_type='narrative' ORDER BY ts DESC LIMIT 1"""
    )
    row = c.fetchone()
    db.close()
    if not row:
        return jsonify({"narrative": None})
    ts, reasoning, raw = row
    try:
        n = json.loads(raw)
    except Exception:
        n = {"dominant_narrative": reasoning, "insight": ""}
    return jsonify({"narrative": n, "ts": ts})


@routes_bp.route("/api/weights")
def api_weights():
    from core.scoring import get_adaptive_weights
    db = get_db()
    c  = db.cursor()
    c.execute(
        """SELECT ts, w_chg, w_rsi, w_vm, w_vol, w_fr, w_holders, w_liq, accuracy
           FROM score_weights ORDER BY ts DESC LIMIT 20"""
    )
    rows = c.fetchall()
    db.close()
    cols = ["ts","w_chg","w_rsi","w_vm","w_vol","w_fr","w_holders","w_liq","accuracy"]
    return jsonify({
        "current": get_adaptive_weights(),
        "history": [dict(zip(cols, r)) for r in rows],
    })


@routes_bp.route("/api/summary")
def api_summary():
    from core.scoring import get_adaptive_weights, get_btc_context
    stats = _db_stats()
    db    = get_db()
    c     = db.cursor()
    c.execute("SELECT MAX(ts) FROM snapshots")
    last_ts = c.fetchone()[0]
    top5_rows = []
    if last_ts:
        c.execute(
            "SELECT sym, score, tier, price, chg, rsi FROM snapshots WHERE ts=%s ORDER BY score DESC LIMIT 5",
            (last_ts,),
        )
        top5_rows = c.fetchall()
    c.execute(
        "SELECT ts, sym, label, score, verified, pct_change, hit FROM alerts ORDER BY ts DESC LIMIT 10"
    )
    recent_alerts = c.fetchall()
    c.execute(
        "SELECT ts, raw_response FROM claude_analyses WHERE analysis_type='narrative' ORDER BY ts DESC LIMIT 1"
    )
    narrative_row = c.fetchone()
    db.close()

    narrative = None
    if narrative_row:
        try:
            narrative = json.loads(narrative_row[1])
        except Exception:
            pass

    return jsonify({
        "version":   "PRO",
        "ts":        int(time.time()),
        "stats":     stats,
        "btc":       get_btc_context(),
        "weights":   get_adaptive_weights(),
        "top5":      [{"sym":r[0],"score":r[1],"tier":r[2],"price":r[3],"chg":r[4],"rsi":r[5]} for r in top5_rows],
        "alerts":    [{"ts":r[0],"sym":r[1],"label":r[2],"score":r[3],"verified":r[4],"pct":r[5],"hit":r[6]} for r in recent_alerts],
        "narrative": narrative,
    })


# ═══════════════════════════════════════
# ROTAS AUTENTICADAS
# ═══════════════════════════════════════

@routes_bp.route("/analyze", methods=["POST"])
@jwt_required()
def analyze():
    """
    POST /analyze
    Body: {"text": "...", "system": "..." (opcional)}
    Retorna análise da IA Claude.
    """
    data   = request.get_json(silent=True) or {}
    text   = (data.get("text") or "").strip()
    system = (data.get("system") or "").strip()

    if not text:
        return jsonify({"error": "Campo 'text' obrigatório"}), 400

    result = analyze_text(text, system=system)
    if not result:
        return jsonify({"error": "IA indisponível — verifique ANTHROPIC_API_KEY"}), 503

    return jsonify({"analysis": result})


@routes_bp.route("/history", methods=["GET"])
@jwt_required()
def history():
    """
    GET /history?limit=50
    Retorna histórico de trades do usuário autenticado.
    """
    user_id = int(get_jwt_identity())
    limit   = min(int(request.args.get("limit", 50)), 200)

    db = get_db()
    c  = db.cursor()
    c.execute(
        """SELECT id, sym, side, qty, price, total_usdt, score, result, order_id, created_at
           FROM trades WHERE user_id=%s ORDER BY created_at DESC LIMIT %s""",
        (user_id, limit),
    )
    rows = c.fetchall()
    db.close()

    cols   = ["id","sym","side","qty","price","total_usdt","score","result","order_id","created_at"]
    trades = [dict(zip(cols, r)) for r in rows]
    return jsonify({"trades": trades, "count": len(trades)})


@routes_bp.route("/config", methods=["POST"])
@jwt_required()
def set_config():
    """
    POST /config
    Salva configurações do usuário (telegram, claude key, binance, etc.)
    NOTA: chaves ficam no banco, NUNCA expostas via API GET.
    """
    user_id = int(get_jwt_identity())
    data    = request.get_json(silent=True) or {}

    allowed = ["telegram_token", "telegram_chat", "claude_key",
               "binance_key", "binance_secret", "real_trading"]
    payload = {k: data[k] for k in allowed if k in data}

    if not payload:
        return jsonify({"error": "Nenhum campo válido enviado"}), 400

    # real_trading só pode ser true se binance_key estiver presente
    if payload.get("real_trading") is True:
        db2 = get_db()
        c2  = db2.cursor()
        c2.execute("SELECT binance_key FROM configs WHERE user_id=%s", (user_id,))
        row2 = c2.fetchone()
        db2.close()
        existing_key = (row2[0] if row2 else "") or payload.get("binance_key", "")
        if not existing_key:
            return jsonify({"error": "Configure binance_key antes de ativar real_trading"}), 400

    try:
        db = get_db()
        c  = db.cursor()
        fields = ", ".join(f"{k}=%s" for k in payload)
        ts = int(time.time())
        # INSERT params: user_id + values + ts
        # UPDATE params: values + ts
        insert_params = [user_id] + list(payload.values()) + [ts] + list(payload.values()) + [ts]
        c.execute(
            f"""INSERT INTO configs (user_id, {', '.join(payload.keys())}, updated_at)
                VALUES (%s, {', '.join(['%s']*len(payload))}, %s)
                ON CONFLICT (user_id) DO UPDATE SET {fields}, updated_at=%s""",
            insert_params,
        )
        db.commit()
        db.close()
        log.info(f"Config atualizada: user_id={user_id}")
        return jsonify({"message": "Configurações salvas"})
    except Exception as e:
        log.error(f"Erro ao salvar config: {e}")
        return jsonify({"error": "Erro interno"}), 500


@routes_bp.route("/config", methods=["GET"])
@jwt_required()
def get_config():
    """
    GET /config
    Retorna configurações do usuário — sem expor as chaves secretas.
    """
    user_id = int(get_jwt_identity())
    db      = get_db()
    c       = db.cursor()
    c.execute(
        "SELECT telegram_chat, real_trading, updated_at, claude_key, binance_key FROM configs WHERE user_id=%s",
        (user_id,),
    )
    row = c.fetchone()
    db.close()
    if not row:
        return jsonify({"config": None})
    return jsonify({
        "config": {
            "telegram_chat":   row[0],
            "real_trading":    row[1],
            "updated_at":      row[2],
            "claude_key_set":  bool(row[3]),
            "binance_key_set": bool(row[4]),
        }
    })


@routes_bp.route("/api/positions", methods=["GET"])
def api_positions():
    """
    GET /api/positions

    Retorna lista de trades da tabela trades_v2.
    Parâmetros opcionais via query string:
      ?status=OPEN      → só posições abertas
      ?status=CLOSED    → só posições fechadas
      ?limit=50         → limite de registros (padrão 50, máx 200)

    Não requer autenticação — dados são de leitura pública do sistema.
    """
    try:
        status = request.args.get("status", "").upper()
        try:
            limit = min(int(request.args.get("limit", 50)), 200)
        except ValueError:
            limit = 50

        db = get_db()
        c  = db.cursor()

        if status in ("OPEN", "CLOSED"):
            c.execute(
                """SELECT id, symbol, entry, size, stop, take_profit,
                          status, pnl, exit_price, label, score, mode,
                          created_at, closed_at
                   FROM trades_v2
                   WHERE status = %s
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (status, limit),
            )
        else:
            c.execute(
                """SELECT id, symbol, entry, size, stop, take_profit,
                          status, pnl, exit_price, label, score, mode,
                          created_at, closed_at
                   FROM trades_v2
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (limit,),
            )

        rows = c.fetchall()
        db.close()

        positions = [
            {
                "id":          r[0],
                "symbol":      r[1],
                "entry":       r[2],
                "size":        r[3],
                "stop":        r[4],
                "take_profit": r[5],
                "status":      r[6],
                "pnl":         r[7],
                "exit_price":  r[8],
                "label":       r[9],
                "score":       r[10],
                "mode":        r[11],
                "created_at":  r[12],
                "closed_at":   r[13],
            }
            for r in rows
        ]

        open_count   = sum(1 for p in positions if p["status"] == "OPEN")
        closed_count = sum(1 for p in positions if p["status"] == "CLOSED")
        total_pnl    = round(sum(p["pnl"] or 0 for p in positions if p["status"] == "CLOSED"), 4)

        return jsonify({
            "positions":    positions,
            "total":        len(positions),
            "open":         open_count,
            "closed":       closed_count,
            "total_pnl":    total_pnl,
        })

    except Exception as e:
        return jsonify({"error": "Erro interno", "detail": str(e)}), 500


# ═══════════════════════════════════════
# ROTAS DE IA — Proxy para Anthropic API
# Usa ANTHROPIC_API_KEY do Railway (env var)
# Frontend chama com JWT — nunca expõe key
# ═══════════════════════════════════════

@routes_bp.route("/api/ai/analyze-token", methods=["POST"])
@jwt_required()
def ai_analyze_token():
    """
    POST /api/ai/analyze-token
    Recebe prompt do token, retorna análise Claude via ANTHROPIC_API_KEY do servidor.
    """
    import urllib.request as _ur
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada no servidor"}), 503

    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "Campo 'prompt' obrigatório"}), 400
    if len(prompt) > 8000:
        return jsonify({"error": "Prompt muito longo"}), 400

    system = (
        "Você é um analista de criptomoedas experiente. "
        "Analise tokens Binance Alpha com base nos dados reais fornecidos. "
        "Seja direto, objetivo e honesto sobre riscos. Responda em português BR."
    )
    payload = json.dumps({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 1000,
        "system":     system,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode()
    req = _ur.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with _ur.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read())
            text = result["content"][0]["text"].strip()
            return jsonify({"text": text})
    except Exception as e:
        log.warning(f"AI analyze-token error: {e}")
        return jsonify({"error": "Falha na IA", "detail": str(e)}), 502


@routes_bp.route("/api/ai/analyze-market", methods=["POST"])
@jwt_required()
def ai_analyze_market():
    """
    POST /api/ai/analyze-market
    Recebe resumo do mercado, retorna análise geral Claude.
    """
    import urllib.request as _ur
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY não configurada no servidor"}), 503

    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"error": "Campo 'prompt' obrigatório"}), 400
    if len(prompt) > 6000:
        return jsonify({"error": "Prompt muito longo"}), 400

    system = (
        "Você é um analista sênior de criptomoedas do mercado Binance Alpha. "
        "Faça análises de mercado objetivas e úteis. Responda em português BR."
    )
    payload = json.dumps({
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 800,
        "system":     system,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode()
    req = _ur.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with _ur.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read())
            text = result["content"][0]["text"].strip()
            return jsonify({"text": text})
    except Exception as e:
        log.warning(f"AI analyze-market error: {e}")
        return jsonify({"error": "Falha na IA", "detail": str(e)}), 502
