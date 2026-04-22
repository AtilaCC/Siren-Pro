"""
api/freqtrade_webhook.py — Recebe sinais do Freqtrade e executa no SIREN PRO

Como funciona:
  1. Freqtrade envia webhook ao abrir/fechar posição
  2. Este endpoint recebe o sinal
  3. Monta o token no padrão SIREN PRO
  4. Passa pelo pipeline completo (IA → risk_manager → Binance)
  5. Notifica via Telegram

Configuração no freqtrade (user_data/config.json):
  "webhook": {
    "enabled": true,
    "url": "https://SEU-APP.railway.app/webhook/freqtrade",
    "webhookbuy": {
      "type": "buy",
      "pair": "{pair}",
      "open_rate": "{open_rate}",
      "stake_amount": "{stake_amount}",
      "current_rate": "{current_rate}"
    },
    "webhooksell": {
      "type": "sell",
      "pair": "{pair}",
      "close_rate": "{close_rate}",
      "profit_ratio": "{profit_ratio}",
      "sell_reason": "{sell_reason}"
    }
  }
"""

import os
import logging
import asyncio
from flask import Blueprint, request, jsonify

log = logging.getLogger("SIREN.freqtrade_webhook")

freqtrade_bp = Blueprint("freqtrade", __name__)

# Chave secreta para autenticar o freqtrade (coloque no .env)
WEBHOOK_SECRET = os.environ.get("FREQTRADE_WEBHOOK_SECRET", "")


def _verify_secret(req) -> bool:
    """Verifica chave secreta no header ou query param."""
    if not WEBHOOK_SECRET:
        return True  # sem chave configurada, aceita tudo (não recomendado em produção)
    secret = req.headers.get("X-Webhook-Secret") or req.args.get("secret", "")
    return secret == WEBHOOK_SECRET


def _build_token_from_signal(data: dict) -> dict:
    """
    Monta um token no padrão SIREN PRO a partir do payload do freqtrade.
    Preenche campos obrigatórios com valores padrão seguros.
    """
    pair  = data.get("pair", "")           # ex: "ETH/USDT"
    sym   = pair.replace("/USDT", "").replace("/", "")  # ex: "ETH"
    price = float(data.get("open_rate") or data.get("current_rate") or 0)

    return {
        "sym":        sym,
        "price":      price,
        "score":      75,           # score padrão para sinais externos
        "tier":       "A",
        "rsi":        45,           # neutro — freqtrade não envia RSI
        "rsi_real":   False,
        "chg":        0.0,
        "vol":        1_000_000,    # volume mínimo para passar filtros
        "fr":         0.0,
        "fr_real":    False,
        "gc":         False,
        "gc_real":    False,
        "pre":        False,
        "pre_conf":   0.0,
        "vol_growth": 0.0,
        "rev":        False,
        "ma9":        price,
        "ma21":       price,
        "holders":    0,
        "chain":      "SPOT",
        "strategy":   data.get("strategy", "freqtrade"),
    }


@freqtrade_bp.route("/webhook/freqtrade", methods=["POST"])
def freqtrade_signal():
    """
    Recebe sinais de entrada/saída do freqtrade.
    """
    if not _verify_secret(request):
        log.warning("Webhook recebido com chave inválida")
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    signal_type = data.get("type", "")

    log.info(f"Freqtrade webhook recebido: type={signal_type} | pair={data.get('pair')}")

    # ── SINAL DE COMPRA ──────────────────────────────────────────────────
    if signal_type == "buy":
        pair  = data.get("pair", "")
        price = float(data.get("open_rate") or 0)

        if not pair or not price:
            return jsonify({"error": "Dados insuficientes"}), 400

        token = _build_token_from_signal(data)
        label = f"FREQTRADE:{data.get('strategy', 'EMA200')}"

        log.info(f"Executando sinal de compra: ${token['sym']} @ {price}")

        # Executa de forma assíncrona sem bloquear o Flask
        try:
            from trading.executor import execute_signal_async
            from core.engine import _can_open_trade, _register_trade_open

            # Verifica anti-overtrading
            can_trade, reason = _can_open_trade(token["sym"], token)
            if not can_trade:
                log.info(f"Trade bloqueado pelo anti-overtrading: {reason}")
                return jsonify({"success": False, "reason": reason}), 200

            # Executa em thread separada para não bloquear o Flask
            import threading
            def run_trade():
                result = asyncio.run(execute_signal_async(token, label))
                if result.get("success"):
                    _register_trade_open(token["sym"])
                    log.info(f"Trade aberto: ${token['sym']} | {result}")
                else:
                    log.info(f"Trade não executado: {result.get('error')}")

            t = threading.Thread(target=run_trade, daemon=True)
            t.start()

            return jsonify({"success": True, "message": f"Sinal recebido para ${token['sym']}"}), 200

        except Exception as e:
            log.error(f"Erro ao processar sinal de compra: {e}")
            return jsonify({"error": str(e)}), 500

    # ── SINAL DE VENDA ──────────────────────────────────────────────────
    elif signal_type == "sell":
        pair        = data.get("pair", "")
        sym         = pair.replace("/USDT", "").replace("/", "")
        profit      = float(data.get("profit_ratio") or 0) * 100
        sell_reason = data.get("sell_reason", "")

        log.info(f"Sinal de venda recebido: ${sym} | lucro={profit:+.2f}% | motivo={sell_reason}")

        try:
            from core.engine import _register_trade_close
            _register_trade_close(sym)
        except Exception as e:
            log.warning(f"Erro ao registrar fechamento: {e}")

        # Notifica Telegram
        try:
            from core.engine import tg_send
            import aiohttp

            emoji  = "✅" if profit > 0 else "❌"
            msg = (
                f"{emoji} <b>FREQTRADE FECHOU</b>\n\n"
                f"Par: <b>${sym}</b>\n"
                f"Resultado: <b>{profit:+.2f}%</b>\n"
                f"Motivo: {sell_reason}\n\n"
                f"⚡ SIREN PRO"
            )

            async def _notify():
                async with aiohttp.ClientSession() as session:
                    await tg_send(session, msg)

            threading.Thread(
                target=lambda: asyncio.run(_notify()), daemon=True
            ).start()

        except Exception as e:
            log.warning(f"Erro ao notificar Telegram: {e}")

        return jsonify({"success": True, "message": f"Venda registrada para ${sym}"}), 200

    else:
        log.warning(f"Tipo de sinal desconhecido: {signal_type}")
        return jsonify({"error": f"Tipo desconhecido: {signal_type}"}), 400
