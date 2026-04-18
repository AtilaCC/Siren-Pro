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

import time
import json
import logging
from flask import Blueprint, request, jsonify
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

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SIREN PRO — Backend Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#0d0d0a; --panel:#141410; --border:#2a2518;
    --amber:#f5a623; --amber2:#ffcc44; --green:#39d98a;
    --red:#ff5c5c; --text:#e8d9b5; --muted:#7a6e57;
  }
  body{background:var(--bg);color:var(--text);font-family:'JetBrains Mono',monospace;margin:0;padding:16px}
  h1{font-family:'Bebas Neue',sans-serif;color:var(--amber);font-size:2rem;margin:0 0 4px}
  .sub{color:var(--muted);font-size:.75rem;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:20px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px}
  .card .val{font-size:1.4rem;font-weight:700;color:var(--amber2)}
  .card .lbl{font-size:.65rem;color:var(--muted);margin-top:2px}
  table{width:100%;border-collapse:collapse;font-size:.72rem}
  th{color:var(--muted);border-bottom:1px solid var(--border);padding:4px 6px;text-align:left}
  td{padding:4px 6px;border-bottom:1px solid #1a1a14}
  tr:hover td{background:#1a1a14}
  .sec{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:16px}
  .sec-title{font-family:'Bebas Neue',sans-serif;color:var(--amber);font-size:1.1rem;margin-bottom:10px}
  .badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:.65rem;font-weight:700}
  .s{background:#3d2e00;color:var(--amber2)}.a{background:#1a3020;color:var(--green)}
  .b{background:#1a1f30;color:#7ab3ff}.c{background:#2a1515;color:var(--red)}
</style>
</head>
<body>
<h1>⚡ SIREN PRO</h1>
<div class="sub" id="last-update">carregando...</div>
<div class="grid" id="stats-grid"></div>
<div class="sec"><div class="sec-title">🏆 TOP TOKENS</div><div id="tokens-table"></div></div>
<div class="sec"><div class="sec-title">🔔 ALERTAS RECENTES</div><div id="alerts-table"></div></div>
<div class="sec" id="narrative-section" style="display:none">
  <div class="sec-title">🧠 NARRATIVA DE MERCADO</div><div id="narrative-content"></div>
</div>
<div class="sec"><div class="sec-title">📊 BACKTEST</div><div id="backtest-table"></div></div>
<script>
function timeAgo(ts){const d=Math.floor(Date.now()/1000-ts);if(d<60)return d+"s";if(d<3600)return Math.floor(d/60)+"m";return Math.floor(d/3600)+"h"}
function fmt(v,d=2){return v==null?"—":(+v).toFixed(d)}
function tierBadge(t){return`<span class="badge ${(t||'c').toLowerCase()}">${t||'?'}</span>`}
async function loadStatus(){
  const d=await fetch("/api/status").then(r=>r.json());
  const a=d.alerts||{};
  document.getElementById("stats-grid").innerHTML=`
    <div class="card"><div class="val">${a.total_alerts||0}</div><div class="lbl">Total Alertas</div></div>
    <div class="card"><div class="val">${a.hits||0}/${a.verified||0}</div><div class="lbl">Acertos</div></div>
    <div class="card"><div class="val">${a.rate_pct||0}%</div><div class="lbl">Win Rate</div></div>
    <div class="card"><div class="val">${a.avg_pct||0}%</div><div class="lbl">Retorno Médio</div></div>
    <div class="card"><div class="val">${a.snapshots||0}</div><div class="lbl">Snapshots</div></div>
    <div class="card"><div class="val" style="color:${d.btc_context?.trend==='bullish'?'var(--green)':d.btc_context?.trend==='bearish'?'var(--red)':'var(--amber)'}">${(d.btc_context?.trend||'?').toUpperCase()}</div><div class="lbl">BTC Trend</div></div>`;
  document.getElementById("last-update").textContent="Atualizado: "+new Date().toLocaleTimeString("pt-BR");
}
async function loadTokens(){
  const d=await fetch("/api/tokens?limit=20").then(r=>r.json());
  if(!d.tokens?.length)return;
  document.getElementById("tokens-table").innerHTML=`<table>
    <thead><tr><th>Token</th><th>Preço</th><th>24h</th><th>RSI</th><th>VM</th><th>Score</th><th>Chain</th></tr></thead>
    <tbody>${d.tokens.map(t=>`<tr>
      <td><b>${t.sym}</b></td><td>$${(+t.price).toPrecision(4)}</td>
      <td style="color:${t.chg>=0?'var(--green)':'var(--red)'}">${fmt(t.chg,1)}%</td>
      <td>${fmt(t.rsi,0)}${t.rsi_real?'✓':'~'}</td>
      <td>${fmt(t.vm,0)}%</td>
      <td>${tierBadge(t.tier)} ${t.score}</td>
      <td style="color:var(--muted)">${t.chain||'?'}</td>
    </tr>`).join("")}</tbody></table>`;
}
async function loadAlerts(){
  const d=await fetch("/api/alerts?limit=15").then(r=>r.json());
  if(!d.alerts?.length)return;
  document.getElementById("alerts-table").innerHTML=`<table>
    <thead><tr><th>Tempo</th><th>Token</th><th>Sinal</th><th>Score</th><th>Verif.</th><th>%</th></tr></thead>
    <tbody>${d.alerts.map(a=>`<tr>
      <td style="color:var(--muted)">${timeAgo(a.ts)}</td><td><b>$${a.sym}</b></td>
      <td>${a.label}</td><td>${a.score}</td>
      <td>${a.verified?(a.hit?'✅':'❌'):'⏳'}</td>
      <td style="color:${(a.pct||0)>=0?'var(--green)':'var(--red)'}">${a.pct!=null?fmt(a.pct,1)+'%':'—'}</td>
    </tr>`).join("")}</tbody></table>`;
}
async function loadBacktest(){
  const d=await fetch("/api/backtest").then(r=>r.json());
  if(!d.results?.length)return;
  document.getElementById("backtest-table").innerHTML=`<table>
    <thead><tr><th>Label</th><th>N</th><th>WR</th><th>Retorno</th><th>Max Gain</th><th>Drawdown</th><th>PF</th><th>Sharpe</th></tr></thead>
    <tbody>${d.results.map(b=>`<tr>
      <td>${b.label}</td><td>${b.n_trades}</td>
      <td style="color:${b.win_rate>=50?'var(--green)':'var(--red)'}">${fmt(b.win_rate,1)}%</td>
      <td style="color:${b.avg_return>=0?'var(--green)':'var(--red)'}">${fmt(b.avg_return,1)}%</td>
      <td style="color:var(--green)">+${fmt(b.avg_max_gain,1)}%</td>
      <td style="color:var(--red)">-${fmt(b.avg_drawdown,1)}%</td>
      <td style="color:${b.profit_factor>=1?'var(--green)':'var(--red)'}">${b.profit_factor}</td>
      <td style="color:${b.sharpe>=1?'var(--green)':'var(--amber)'}">${b.sharpe}</td>
    </tr>`).join("")}</tbody></table>`;
}
async function loadNarrative(){
  const d=await fetch("/api/narrative").then(r=>r.json());
  if(!d.narrative)return;
  document.getElementById("narrative-section").style.display="";
  const n=d.narrative;
  document.getElementById("narrative-content").innerHTML=`
    <div style="margin-bottom:8px">
      <span style="font-size:1.1rem;color:var(--amber2);font-family:'Bebas Neue',sans-serif">${n.dominant_narrative||"—"}</span>
      <span style="color:var(--muted);font-size:.75rem;margin-left:8px">• ${n.hot_chain||"?"} • ${n.confidence||0}% confiança</span>
    </div>
    <div style="font-size:.78rem">${n.insight||""}</div>
    ${n.tokens_in_narrative?`<div style="font-size:.72rem;color:var(--muted);margin-top:4px">Tokens: ${n.tokens_in_narrative.join(", ")}</div>`:""}
    <div style="font-size:.65rem;color:var(--muted);margin-top:8px">${timeAgo(d.ts)} atrás</div>`;
}
async function init(){await Promise.allSettled([loadStatus(),loadTokens(),loadAlerts(),loadBacktest(),loadNarrative()]);}
init();setInterval(init,60000);
</script>
</body>
</html>"""


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
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


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
        values = list(payload.values()) + [int(time.time()), user_id]
        c.execute(
            f"""INSERT INTO configs (user_id, {', '.join(payload.keys())}, updated_at)
                VALUES (%s, {', '.join(['%s']*len(payload))}, %s)
                ON CONFLICT (user_id) DO UPDATE SET {fields}, updated_at=%s""",
            [user_id] + list(payload.values()) + [int(time.time())] + values,
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
        "SELECT telegram_chat, real_trading, updated_at FROM configs WHERE user_id=%s",
        (user_id,),
    )
    row = c.fetchone()
    db.close()
    if not row:
        return jsonify({"config": None})
    return jsonify({
        "config": {
            "telegram_chat": row[0],
            "real_trading":  row[1],
            "updated_at":    row[2],
            "binance_key_set":  True,   # não expõe a chave, apenas informa se existe
            "claude_key_set":   True,
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
