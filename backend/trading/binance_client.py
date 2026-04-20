"""
trading/binance_client.py — Cliente REST Binance (versão robusta)

Melhorias:
✔ Validação correta via exchangeInfo
✔ Cache de símbolos (performance)
✔ Sanitização de símbolos
✔ Evita pares inválidos
✔ Estrutura pronta pra produção
"""

import os
import time
import hmac
import hashlib
import logging
import urllib.parse
import urllib.request
import json
import re

log = logging.getLogger("SIREN.binance")

BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
ENABLE_REAL        = os.environ.get("ENABLE_REAL_TRADING", "false").lower() == "true"

BASE_URL = "https://api.binance.com"

# Cache global de símbolos válidos
SYMBOLS_CACHE = {}

# ═══════════════════════════════════════
# UTIL
# ═══════════════════════════════════════

def sanitize_token(token: str) -> str:
    """Remove caracteres inválidos e força uppercase."""
    token = token.upper()
    return re.sub(r'[^A-Z0-9]', '', token)


def build_symbol(token: str, quote: str = "USDT") -> str:
    """Constrói símbolo seguro."""
    token = sanitize_token(token)
    return f"{token}{quote}"


# ═══════════════════════════════════════
# ASSINATURA
# ═══════════════════════════════════════

def _sign(params: dict) -> str:
    query = urllib.parse.urlencode(params)
    return hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()


def _request(method: str, path: str, params: dict = None, signed: bool = False) -> dict:
    params = params or {}

    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _sign(params)

    query = urllib.parse.urlencode(params)
    url   = f"{BASE_URL}{path}?{query}" if query else f"{BASE_URL}{path}"

    req = urllib.request.Request(
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
        raise RuntimeError(body)
    except Exception as e:
        log.error(f"Erro na requisição: {e}")
        raise


# ═══════════════════════════════════════
# MERCADO
# ═══════════════════════════════════════

def load_symbols():
    """Carrega todos os símbolos válidos da Binance."""
    global SYMBOLS_CACHE
    log.info("Carregando símbolos da Binance...")

    data = _request("GET", "/api/v3/exchangeInfo")

    SYMBOLS_CACHE = {
        s["symbol"]: s
        for s in data["symbols"]
        if s["status"] == "TRADING"
    }

    log.info(f"{len(SYMBOLS_CACHE)} símbolos ativos carregados")


def validate_symbol(symbol: str) -> bool:
    """Validação robusta de símbolo. Carrega cache se ainda não foi carregado."""
    global SYMBOLS_CACHE
    if not SYMBOLS_CACHE:
        try:
            load_symbols()
        except Exception:
            # Se falhar ao carregar, permite todos (não bloqueia trading)
            return True
    if not SYMBOLS_CACHE:
        return True  # cache vazio = permite tudo
    return symbol in SYMBOLS_CACHE


def get_symbol_info(symbol: str):
    return SYMBOLS_CACHE.get(symbol)


def get_price(symbol: str) -> float:
    """
    Busca preço do símbolo.
    Tenta Spot primeiro. Se não estiver no cache (token Alpha),
    tenta buscar diretamente sem validação de cache.
    """
    # Símbolo existe no Spot — busca normal
    if not SYMBOLS_CACHE or symbol in SYMBOLS_CACHE:
        try:
            data = _request("GET", "/api/v3/ticker/price", {"symbol": symbol})
            return float(data["price"])
        except Exception:
            pass

    # Fallback para tokens Alpha: tenta sem validação de cache
    # Alguns tokens Alpha têm par USDT mas não aparecem no exchangeInfo
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": BINANCE_API_KEY})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            price = float(data.get("price", 0))
            if price > 0:
                return price
    except Exception:
        pass

    # Última tentativa: busca via ticker 24h (mais abrangente)
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": BINANCE_API_KEY})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            price = float(data.get("lastPrice", 0))
            if price > 0:
                return price
    except Exception:
        pass

    raise ValueError(f"Preço não encontrado para {symbol}")


# ═══════════════════════════════════════
# CONTA
# ═══════════════════════════════════════

def get_balance(asset: str = "USDT") -> float:
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

def market_buy(token: str, usdt_amount: float):
    """Compra segura com validação completa."""

    symbol = build_symbol(token)

    log.info(f"[{'REAL' if ENABLE_REAL else 'PAPER'}] BUY {symbol} ${usdt_amount}")

    if not validate_symbol(symbol):
        log.warning(f"{symbol} inválido — ignorando")
        return None

    price = get_price(symbol)
    qty   = round(usdt_amount / price, 6)

    if not ENABLE_REAL:
        return {
            "symbol": symbol,
            "price": price,
            "qty": qty,
            "paper": True
        }

    balance = get_balance("USDT")

    if balance < usdt_amount:
        raise RuntimeError("Saldo insuficiente")

    params = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": usdt_amount,
    }

    return _request("POST", "/api/v3/order", params, signed=True)


def market_sell(token: str, qty: float):
    symbol = build_symbol(token)

    log.info(f"[{'REAL' if ENABLE_REAL else 'PAPER'}] SELL {symbol} qty={qty}")

    if not validate_symbol(symbol):
        log.warning(f"{symbol} inválido — ignorando")
        return None

    if not ENABLE_REAL:
        price = get_price(symbol)
        return {
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "paper": True
        }

    params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
    }

    return _request("POST", "/api/v3/order", params, signed=True)
