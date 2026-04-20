"""
trading/binance_client.py — Cliente REST Binance para auto trade.

Funcionalidades:
  - get_balance()     : saldo disponível em USDT
  - get_price()       : preço atual de um par
  - validate_symbol() : valida se símbolo existe na Binance
  - market_buy()      : ordem de compra a mercado
  - market_sell()     : ordem de venda a mercado
  - get_order()       : status de uma ordem

Modo SIMULAÇÃO ativo por padrão (ENABLE_REAL_TRADING=false).
"""

import os
import time
import hmac
import hashlib
import logging
import urllib.parse
import urllib.request
import json

log = logging.getLogger("SIREN.binance")

BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
ENABLE_REAL        = os.environ.get("ENABLE_REAL_TRADING", "false").lower() == "true"

BASE_URL = "https://api.binance.com"


# ═══════════════════════════════════════
# ASSINATURA
# ═══════════════════════════════════════

def _sign(params: dict) -> str:
    """Gera assinatura HMAC-SHA256 para requisições privadas."""
    query = urllib.parse.urlencode(params)
    return hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()


def _request(method: str, path: str, params: dict = None, signed: bool = False) -> dict:
    """
    Executa requisição HTTP na Binance REST API.
    Adiciona timestamp e assinatura se signed=True.
    """
    params = params or {}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _sign(params)

    query  = urllib.parse.urlencode(params)
    url    = f"{BASE_URL}{path}?{query}" if query else f"{BASE_URL}{path}"
    req    = urllib.request.Request(
        url,
        method=method,
        headers={
            "X-MBX-APIKEY": BINANCE_API_KEY,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.error(f"Binance HTTP {e.code}: {body}")
        raise RuntimeError(f"Binance API error {e.code}: {body}")
    except Exception as e:
        log.error(f"Binance request falhou: {e}")
        raise


# ═══════════════════════════════════════
# INFORMAÇÕES DE MERCADO
# ═══════════════════════════════════════

def get_price(symbol: str) -> float:
    """Retorna o preço atual de um par (ex: BTCUSDT)."""
    data = _request("GET", "/api/v3/ticker/price", {"symbol": symbol})
    return float(data["price"])


# Cache de símbolos válidos para evitar chamadas repetidas
_valid_symbols_cache: set = set()
_symbols_cache_ts: float = 0
_SYMBOLS_CACHE_TTL = 3600  # 1 hora

def _load_all_symbols() -> set:
    """Carrega todos os símbolos USDT ativos da Binance e cacheia."""
    global _valid_symbols_cache, _symbols_cache_ts
    import time as _time
    now = _time.time()
    if _valid_symbols_cache and (now - _symbols_cache_ts) < _SYMBOLS_CACHE_TTL:
        return _valid_symbols_cache
    try:
        data = _request("GET", "/api/v3/exchangeInfo", {})
        symbols = data.get("symbols", [])
        _valid_symbols_cache = {
            s["symbol"] for s in symbols
            if s["status"] == "TRADING" and s["symbol"].endswith("USDT")
        }
        _symbols_cache_ts = now
        log.info(f"Símbolos Binance carregados: {len(_valid_symbols_cache)} pares USDT ativos")
        return _valid_symbols_cache
    except Exception as e:
        log.warning(f"Erro ao carregar símbolos: {e}")
        return _valid_symbols_cache  # retorna cache antigo se houver

def validate_symbol(symbol: str) -> bool:
    """Verifica se o símbolo existe e está ativo na Binance."""
    try:
        valid = _load_all_symbols()
        if valid:
            return symbol in valid
        # Fallback: tenta buscar preço diretamente
        get_price(symbol)
        return True
    except Exception:
        return False


def get_symbol_info(symbol: str) -> dict | None:
    """Retorna informações de filtros do símbolo (minQty, stepSize, etc)."""
    try:
        data = _request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        return None
    except Exception:
        return None


# ═══════════════════════════════════════
# CONTA
# ═══════════════════════════════════════

def get_balance(asset: str = "USDT") -> float:
    """Retorna saldo disponível do ativo informado."""
    if not BINANCE_API_KEY:
        return 0.0
    data = _request("GET", "/api/v3/account", signed=True)
    for b in data.get("balances", []):
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0


# ═══════════════════════════════════════
# ORDENS
# ═══════════════════════════════════════

def _round_qty(qty: float, step_size: float) -> float:
    """Arredonda quantidade para o stepSize do par."""
    import math
    precision = int(round(-math.log10(step_size)))
    return round(math.floor(qty / step_size) * step_size, precision)


def market_buy(symbol: str, usdt_amount: float) -> dict:
    """
    Executa ordem de compra a mercado.
    SIMULAÇÃO: retorna mock se ENABLE_REAL_TRADING=false.
    """
    log.info(f"[{'REAL' if ENABLE_REAL else 'PAPER'}] BUY {symbol} ${usdt_amount:.2f}")

    if not ENABLE_REAL:
        # Paper trading: simula resposta
        price = get_price(symbol)
        qty   = round(usdt_amount / price, 6)
        return {
            "orderId":        f"PAPER_{int(time.time())}",
            "symbol":         symbol,
            "side":           "BUY",
            "type":           "MARKET",
            "status":         "FILLED",
            "executedQty":    str(qty),
            "cummulativeQuoteQty": str(usdt_amount),
            "paper":          True,
        }

    # Validações antes de operar real
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        raise RuntimeError("Chaves Binance não configuradas")

    if not validate_symbol(symbol):
        raise ValueError(f"Símbolo inválido ou não negociável: {symbol}")

    balance = get_balance("USDT")
    if balance < usdt_amount:
        raise RuntimeError(f"Saldo insuficiente: ${balance:.2f} < ${usdt_amount:.2f}")

    params = {
        "symbol":     symbol,
        "side":       "BUY",
        "type":       "MARKET",
        "quoteOrderQty": usdt_amount,
    }
    return _request("POST", "/api/v3/order", params, signed=True)


def market_sell(symbol: str, qty: float) -> dict:
    """
    Executa ordem de venda a mercado.
    SIMULAÇÃO: retorna mock se ENABLE_REAL_TRADING=false.
    """
    log.info(f"[{'REAL' if ENABLE_REAL else 'PAPER'}] SELL {symbol} qty={qty}")

    if not ENABLE_REAL:
        price = get_price(symbol)
        return {
            "orderId":        f"PAPER_{int(time.time())}",
            "symbol":         symbol,
            "side":           "SELL",
            "type":           "MARKET",
            "status":         "FILLED",
            "executedQty":    str(qty),
            "cummulativeQuoteQty": str(qty * price),
            "paper":          True,
        }

    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        raise RuntimeError("Chaves Binance não configuradas")

    if not validate_symbol(symbol):
        raise ValueError(f"Símbolo inválido: {symbol}")

    params = {
        "symbol":   symbol,
        "side":     "SELL",
        "type":     "MARKET",
        "quantity": qty,
    }
    return _request("POST", "/api/v3/order", params, signed=True)


def get_order(symbol: str, order_id: str) -> dict:
    """Consulta status de uma ordem."""
    params = {"symbol": symbol, "orderId": order_id}
    return _request("GET", "/api/v3/order", params, signed=True)
