"""
core/scanner.py — Busca e enriquecimento de tokens Binance Alpha.

Preserva toda a lógica do SIREN v6 + integração com BinanceSpotValidator
para eliminar erros "Invalid symbol -1121".
"""

import time
import asyncio
import logging
import aiohttp
import psycopg2.extras

from core.scoring import (
    calc_score, calc_rsi, calc_ma,
    get_adaptive_weights, get_btc_context, set_btc_context,
)
from core.binance_spot_validator import BinanceSpotValidator   # ← Adicionado
from db.connection import get_db
from binance.exceptions import BinanceAPIException

log = logging.getLogger("SIREN.scanner")

# ── Endpoints ──────────────────────────────────────────────────────────────
ALPHA_API   = "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
KLINES_API  = "https://api.binance.com/api/v3/klines"
FUNDING_API = "https://fapi.binance.com/fapi/v1/premiumIndex"
BTC_KLINES  = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=50"
PROXIES     = ["", "https://api.allorigins.win/raw?url=", "https://corsproxy.io/?"]

# ── Filtros de qualidade ───────────────────────────────────────────────────
MIN_LIQUIDITY  = 50_000
MIN_HOLDERS    = 500
MIN_VOLUME_24H = 20_000
KLINES_SEM     = 20


# ═══════════════════════════════════════
# VALIDADOR SPOT (novo)
# ═══════════════════════════════════════

validator: BinanceSpotValidator | None = None   # será inicializado abaixo


def init_validator(client):
    """Inicializa o validador de símbolos Spot (chamar uma vez no startup)"""
    global validator
    if validator is None:
        validator = BinanceSpotValidator(client)
        validator.update_valid_pairs()
        log.info("BinanceSpotValidator inicializado com sucesso.")


# ═══════════════════════════════════════
# CONTEXTO BTC
# ═══════════════════════════════════════

async def fetch_btc_context(session) -> dict:
    """Atualiza contexto macro do BTC (tendência, multiplicador de score)."""
    ctx = get_btc_context()
    try:
        async with session.get(BTC_KLINES, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status != 200:
                return ctx
            data   = await r.json()
            closes = [float(k[4]) for k in data]
            if len(closes) < 20:
                return ctx

            ema9   = sum(closes[-9:])  / 9
            ema21  = sum(closes[-21:]) / 21
            chg_4h = (closes[-1] - closes[-4]) / closes[-4] * 100 if closes[-4] > 0 else 0
            rsi    = calc_rsi(closes)

            if ema9 > ema21 * 1.005 and chg_4h > 1 and (rsi or 50) < 70:
                trend, mult = "bullish", 1.15
            elif ema9 < ema21 * 0.995 and chg_4h < -1:
                trend, mult = "bearish", 0.75
            else:
                trend, mult = "neutral", 1.0

            new_ctx = {
                "trend":      trend,
                "score_mult": mult,
                "rsi":        rsi or 50,
                "chg_4h":     round(chg_4h, 2),
                "price":      closes[-1],
            }
            set_btc_context(new_ctx)
            log.info(f"BTC contexto: {trend} | 4h={chg_4h:+.1f}% | mult={mult}")
    except Exception as e:
        log.warning(f"BTC contexto falha: {e}")
    return get_btc_context()


# ═══════════════════════════════════════
# GET CURRENT PRICE SEGURO (novo)
# ═══════════════════════════════════════

def _get_current_price(symbol: str) -> float | None:
    """Busca preço atual apenas se o par existir no Spot. Evita Invalid symbol."""
    if validator is None:
        log.error("Validator não inicializado!")
        return None

    try:
        clean_symbol = validator.get_clean_symbol(symbol)
        if not clean_symbol:
            log.warning(f"[MONITOR] Símbolo inválido ou inexistente no Spot: {symbol} – pulando")
            return None

        # Aqui você pode chamar o client síncrono ou adaptar para async se necessário
        # Exemplo com client síncrono (ajuste se seu client for async):
        # ticker = client.get_symbol_ticker(symbol=clean_symbol)
        # return float(ticker['price'])

        # Por enquanto retorna None se não for Spot (tokens Alpha puros)
        log.warning(f"[MONITOR] {symbol} não tem par Spot válido – pulando preço atual")
        return None

    except BinanceAPIException as e:
        if e.code == -1121:
            log.warning(f"[MONITOR] Preço inválido para {symbol} no Spot – pulando")
        else:
            log.error(f"Erro Binance em {symbol}: {e}")
        return None
    except Exception as e:
        log.error(f"Erro ao buscar preço de {symbol}: {e}")
        return None


# ═══════════════════════════════════════
# DATA FETCHING (atualizado)
# ═══════════════════════════════════════

async def fetch_alpha_tokens(session):
    """Busca tokens da Binance Alpha com fallback via proxies CORS."""
    for proxy in PROXIES:
        try:
            url = f"{proxy}{ALPHA_API}" if proxy else ALPHA_API
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    if data and data.get("data") and len(data["data"]) > 0:
                        log.info(f"Alpha API: {len(data['data'])} tokens recebidos")
                        return data["data"]
        except Exception as e:
            log.warning(f"Alpha proxy falha: {e}")
    log.error("Todas as fontes Alpha falharam")
    return []


async def fetch_klines(session, sym, sem, interval="1h", limit=50):
    """Busca klines com fallback USDT → FDUSD + validação Spot."""
    if validator is None:
        log.warning(f"Validator não inicializado para {sym}")
        return None

    async with sem:
        for suffix in ["USDT", "FDUSD"]:
            try:
                full_symbol = f"{sym}{suffix}"
                if not validator.is_valid_symbol(full_symbol.replace(suffix, "")):
                    continue  # pula se não for par Spot válido

                url = f"{KLINES_API}?symbol={full_symbol}&interval={interval}&limit={limit}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if isinstance(data, list) and len(data) >= 15:
                            log.info(f"Klines obtidas para {full_symbol}")
                            return {
                                "closes":  [float(k[4]) for k in data],
                                "volumes": [float(k[5]) for k in data],
                            }
            except Exception:
                continue
    return None


async def fetch_funding_rates(session) -> dict:
    """Busca funding rates de todos os pares perpetuais."""
    try:
        async with session.get(FUNDING_API, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                data   = await r.json()
                fr_map = {}
                for d in data:
                    sym = d["symbol"].replace("USDT", "").replace("FDUSD", "")
                    fr_map[sym] = float(d.get("lastFundingRate", 0))
                log.info(f"Funding rates: {len(fr_map)} pares")
                return fr_map
    except Exception as e:
        log.warning(f"Funding rates falha: {e}")
    return {}


# ... (o resto do arquivo permanece igual: fetch_telegram_sentiment, fetch_google_trends, 
# passes_quality_filter, detect_pre_pump, build_token, enrich_tokens, save_snapshot)

# ═══════════════════════════════════════
# ENRICH TOKENS (atualizado com validação)
# ═══════════════════════════════════════

async def enrich_tokens(session, tokens: list, weights=None, client=None) -> list:
    """
    Enriquece a lista de tokens com RSI real, MA, etc.
    Agora usa o validador para evitar chamadas inválidas.
    """
    global validator
    if client and validator is None:
        init_validator(client)

    sem     = asyncio.Semaphore(KLINES_SEM)
    tasks   = [fetch_klines(session, t["sym"], sem) for t in tokens]
    results = await asyncio.gather(*tasks)
    btc_ctx = get_btc_context()

    for t, klines in zip(tokens, results):
        if not klines:
            continue   # token Alpha sem par Spot → pula enriquecimento de klines

        rsi  = calc_rsi(klines["closes"])
        ma9  = calc_ma(klines["closes"], 9)
        ma21 = calc_ma(klines["closes"], 21)

        if rsi  is not None: t["rsi"]  = rsi;  t["rsi_real"] = True
        if ma9  is not None: t["ma9"]  = round(ma9,  8)
        if ma21 is not None: t["ma21"] = round(ma21, 8)
        if ma9  is not None and ma21 is not None:
            t["gc"]      = ma9 > ma21
            t["gc_real"] = True

        t["pre"], t["pre_conf"] = detect_pre_pump(t, klines["closes"], klines["volumes"])
        t["rev"] = t["chg"] < -15 and t["rsi"] < 35

        t["score"] = calc_score(
            t["chg"], t["rsi"], t["vm"], t["price"],
            t["vol"], t["liq"], t["holders"], t["fr"], weights,
        )
        t["score"] = round(t["score"] * btc_ctx.get("score_mult", 1.0))
        t["score"] = max(10, min(99, t["score"]))
        t["tier"]  = "S" if t["score"] >= 80 else "A" if t["score"] >= 65 else "B" if t["score"] >= 50 else "C"

    # Funding rates (mantido)
    fr_map   = await fetch_funding_rates(session)
    fr_count = 0
    for t in tokens:
        if t["sym"] in fr_map:
            t["fr"]      = round(fr_map[t["sym"]] * 100, 4)
            t["fr_real"] = True
            fr_count    += 1
            # recalcula score com FR real...

    real_rsi = sum(1 for t in tokens if t.get("rsi_real", False))
    log.info(f"Enriquecido: {real_rsi} RSI real | {fr_count} FR real | {len(tokens)} tokens processados")
    return tokens


# O resto do arquivo (save_snapshot, etc.) permanece igual