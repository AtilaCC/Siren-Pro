"""
realtime/ws.py — Estrutura de WebSocket para sinais em tempo real.

Implementação SSE (Server-Sent Events) via Flask, pronta para uso.
Os sinais são publicados internamente via _signal_queue e consumidos
pelos clientes conectados em GET /ws/signals.

Como usar:
  1. O engine chama publish_signal(token, label) ao detectar um alerta.
  2. O frontend conecta em GET /ws/signals e recebe eventos em tempo real.
"""

import json
import time
import queue
import logging
import threading
from flask import Blueprint, Response, stream_with_context

log   = logging.getLogger("SIREN.ws")
ws_bp = Blueprint("ws", __name__)

# Fila de sinais compartilhada entre threads
_signal_queue: queue.Queue = queue.Queue(maxsize=500)

# Lista de filas de clientes conectados (uma por cliente SSE)
_clients: list = []
_clients_lock  = threading.Lock()


# ═══════════════════════════════════════
# PUBLICAÇÃO INTERNA
# ═══════════════════════════════════════

def publish_signal(token: dict, label: str):
    """
    Chamado pelo engine para publicar um novo sinal.
    Envia o evento para todos os clientes SSE conectados.
    """
    event = {
        "type":  "signal",
        "label": label,
        "ts":    int(time.time()),
        "sym":   token.get("sym"),
        "score": token.get("score"),
        "tier":  token.get("tier"),
        "price": token.get("price"),
        "chg":   token.get("chg"),
        "rsi":   token.get("rsi"),
        "chain": token.get("chain"),
    }
    payload = f"data: {json.dumps(event)}\n\n"

    with _clients_lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)

    log.debug(f"Signal publicado: {label} ${token.get('sym')} → {len(_clients)} clientes")


def publish_btc_update(ctx: dict):
    """Publica atualização do contexto BTC para clientes SSE."""
    event   = {"type": "btc", "ts": int(time.time()), **ctx}
    payload = f"data: {json.dumps(event)}\n\n"
    with _clients_lock:
        for q in _clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


# ═══════════════════════════════════════
# ENDPOINT SSE
# ═══════════════════════════════════════

@ws_bp.route("/ws/signals")
def sse_signals():
    """
    GET /ws/signals
    Stream SSE. O cliente conecta e recebe eventos em tempo real.

    Exemplo de uso no frontend:
      const es = new EventSource('/ws/signals');
      es.onmessage = (e) => {
        const signal = JSON.parse(e.data);
        console.log(signal);
      };
    """
    client_q: queue.Queue = queue.Queue(maxsize=100)
    with _clients_lock:
        _clients.append(client_q)
    log.info(f"Cliente SSE conectado. Total: {len(_clients)}")

    def generate():
        # Heartbeat inicial
        yield "data: {\"type\":\"connected\",\"ts\":" + str(int(time.time())) + "}\n\n"
        try:
            while True:
                try:
                    # Espera até 25s por evento, depois envia heartbeat
                    msg = client_q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    # Heartbeat para manter conexão viva
                    yield f"data: {{\"type\":\"ping\",\"ts\":{int(time.time())}}}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _clients_lock:
                if client_q in _clients:
                    _clients.remove(client_q)
            log.info(f"Cliente SSE desconectado. Restantes: {len(_clients)}")

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@ws_bp.route("/ws/status")
def sse_status():
    """GET /ws/status — retorna número de clientes SSE conectados."""
    with _clients_lock:
        count = len(_clients)
    return {"connected_clients": count, "ts": int(time.time())}
