"""
core/scanner.py — Busca e enriquecimento de tokens Binance Alpha.

Preserva toda a lógica do SIREN v6:
  - fetch_alpha_tokens()  : busca tokens via Binance Alpha API
  - fetch_klines()        : klines com fallback USDT/FDUSD
  - fetch_funding_rates() : taxas de financiamento
  - fetch_btc_context()   : contexto macro BTC
  - enrich_tokens()       : enriquece com RSI real, MA, GC, pre-pump, FR
  - build_token()         : constrói objeto de token a partir do raw API
  - passes_quality_filter(): filtro anti-scam
  - detect_pre_pump()     : detecção avançada de pré-pump
  - save_snapshot()       : persiste snapshot no PostgreSQL
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
from db.connection import get_db

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
KLINES_SEM     = 8


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
# DATA FETCHING
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
                        log.info(f"Alpha API: {len(data['data'])} tokens")
                        return data["data"]
        except Exception as e:
            log.warning(f"Alpha proxy falha: {e}")
    log.error("Todas as fontes Alpha falharam")
    return []


async def fetch_klines(session, sym, sem, interval="1h", limit=50):
    """Busca klines com fallback USDT → FDUSD."""
    async with sem:
        for suffix in ["USDT", "FDUSD"]:
            try:
                url = f"{KLINES_API}?symbol={sym}{suffix}&interval={interval}&limit={limit}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if isinstance(data, list) and len(data) >= 15:
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
                log.info(f"Funding rates: {len(fr_map)}")
                return fr_map
    except Exception as e:
        log.warning(f"Funding rates falha: {e}")
    return {}


# ═══════════════════════════════════════
# FILTRO ANTI-SCAM
# ═══════════════════════════════════════

def passes_quality_filter(t: dict) -> tuple:
    """Retorna (True, '') ou (False, motivo)."""
    if t["liq"] < MIN_LIQUIDITY:
        return False, f"liquidez baixa (${t['liq']:,.0f} < ${MIN_LIQUIDITY:,})"
    if t["holders"] < MIN_HOLDERS:
        return False, f"holders insuficientes ({t['holders']} < {MIN_HOLDERS})"
    if t["vol"] < MIN_VOLUME_24H:
        return False, f"volume baixo (${t['vol']:,.0f} < ${MIN_VOLUME_24H:,})"
    if t["mcap"] <= 0:
        return False, "mcap inválido"
    if abs(t["chg"]) > 1000:
        return False, f"variação suspeita ({t['chg']:.0f}%)"
    return True, ""


# ═══════════════════════════════════════
# PRÉ-PUMP AVANÇADO
# ═══════════════════════════════════════

def detect_pre_pump(t: dict, closes: list, volumes: list) -> tuple:
    """
    Detecta setup de pré-pump usando:
      - Crescimento de volume (3 barras vs média 10)
      - Compressão de Bollinger Bands
      - RSI em zona de acumulação (32–58)
      - Preço flat com volume crescente
    Retorna (is_pre_pump: bool, confidence: float)
    """
    if not closes or len(closes) < 20 or not volumes or len(volumes) < 20:
        return t.get("pre", False), 0.5

    confidence = 0.0

    # Crescimento de volume
    vol_avg10  = sum(volumes[-10:]) / 10
    vol_rec3   = sum(volumes[-3:])  / 3
    vol_growth = (vol_rec3 - vol_avg10) / vol_avg10 if vol_avg10 > 0 else 0
    if vol_growth > 0.5:   confidence += 0.30
    elif vol_growth > 0.2: confidence += 0.15

    # Compressão Bollinger
    sma20    = sum(closes[-20:]) / 20
    std20    = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5
    bb_width = (4 * std20) / sma20 if sma20 > 0 else 0
    if bb_width < 0.04:   confidence += 0.25
    elif bb_width < 0.08: confidence += 0.12

    # RSI zona de acumulação
    rsi = t.get("rsi", 50)
    if 32 <= rsi <= 48:  confidence += 0.25
    elif 48 < rsi <= 58: confidence += 0.10

    # Preço flat + volume crescente
    price_chg_3 = abs(closes[-1] - closes[-3]) / closes[-3] if closes[-3] > 0 else 1
    if price_chg_3 < 0.02 and vol_growth > 0.2:
        confidence += 0.20

    base_cond = -5 < t["chg"] < 12 and t["score"] >= 55 and not t.get("gc", False)
    is_pre    = base_cond and confidence >= 0.45

    t["vol_growth"]        = round(vol_growth * 100, 1)
    t["price_compression"] = round(bb_width * 100, 2)

    return is_pre, round(confidence, 2)


# ═══════════════════════════════════════
# BUILD + ENRICH TOKENS
# ═══════════════════════════════════════

def build_token(d: dict, weights=None) -> dict | None:
    """Constrói objeto de token normalizado a partir do payload raw da API."""
    try:
        price   = float(d.get("price", 0) or 0)
        chg     = float(d.get("percentChange24h", 0) or 0)
        vol     = float(d.get("volume24h", 0) or 0)
        mcap    = float(d.get("marketCap", 0) or 0)
        liq     = float(d.get("liquidity", 0) or 0)
        holders = int(d.get("holders", 0) or 0)
        sym     = (d.get("symbol") or d.get("name") or "???").upper()
        name    = d.get("name", sym)
        chain   = (d.get("chainName") or "").lower()
        hot     = d.get("hotTag", False)

        rsi   = max(10, min(90, round(50 + chg * 1.2)))
        vm    = min(100, round((vol / mcap) * 100)) if mcap > 0 else 0
        fr    = round(-chg * 0.003, 4)
        score = calc_score(chg, rsi, vm, price, vol, liq, holders, fr, weights)
        tier  = "S" if score >= 80 else "A" if score >= 65 else "B" if score >= 50 else "C"

        listed_at = d.get("listedAt") or d.get("listTime") or 0
        days = int((time.time() * 1000 - listed_at) / 86400000) if listed_at else None

        return {
            "sym": sym, "name": name, "price": price, "chg": chg,
            "vol": vol, "mcap": mcap, "liq": liq, "holders": holders,
            "rsi": rsi, "rsi_real": False, "vm": vm, "score": score,
            "tier": tier, "gc": chg > 5 and vm > 30, "gc_real": False,
            "ma9": None, "ma21": None, "rev": chg < -15 and rsi < 35,
            "hot": hot, "pre": False, "pre_conf": 0.0, "chain": chain,
            "fr": fr, "fr_real": False, "days": days,
            "vol_growth": 0.0, "price_compression": 0.0,
        }
    except Exception:
        return None


async def enrich_tokens(session, tokens: list, weights=None) -> list:
    """
    Enriquece a lista de tokens com:
      RSI real, MA9/MA21, Golden Cross, pré-pump, reversão e Funding Rate.
    """
    sem     = asyncio.Semaphore(KLINES_SEM)
    tasks   = [fetch_klines(session, t["sym"], sem) for t in tokens]
    results = await asyncio.gather(*tasks)
    btc_ctx = get_btc_context()

    for t, klines in zip(tokens, results):
        if not klines:
            continue
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

    fr_map   = await fetch_funding_rates(session)
    fr_count = 0
    for t in tokens:
        if t["sym"] in fr_map:
            t["fr"]      = round(fr_map[t["sym"]] * 100, 4)
            t["fr_real"] = True
            fr_count    += 1
            t["score"]   = calc_score(
                t["chg"], t["rsi"], t["vm"], t["price"],
                t["vol"], t["liq"], t["holders"], t["fr"], weights,
            )
            t["score"] = round(t["score"] * btc_ctx.get("score_mult", 1.0))
            t["score"] = max(10, min(99, t["score"]))
            t["tier"]  = "S" if t["score"] >= 80 else "A" if t["score"] >= 65 else "B" if t["score"] >= 50 else "C"

    real_rsi = sum(1 for t in tokens if t["rsi_real"])
    log.info(f"Enriquecido: {real_rsi} RSI real | {fr_count} FR real de {len(tokens)} tokens")
    return tokens


# ═══════════════════════════════════════
# SNAPSHOT
# ═══════════════════════════════════════

def save_snapshot(tokens: list):
    """Persiste até 200 tokens no banco e limpa snapshots com mais de 60 dias."""
    db  = get_db()
    c   = db.cursor()
    ts  = int(time.time())
    rows = []
    for t in tokens[:200]:
        rows.append((
            ts, t["sym"], t["price"], t["chg"], t["vol"], t["mcap"],
            t["liq"], t["holders"], t["rsi"], 1 if t["rsi_real"] else 0,
            t["ma9"], t["ma21"], 1 if t["gc"] else 0,
            t["score"], t["tier"], t["fr"], 1 if t["fr_real"] else 0,
            t["chain"], t["vm"], t.get("vol_growth", 0), t.get("price_compression", 0),
        ))

    psycopg2.extras.execute_values(
        c,
        """INSERT INTO snapshots
           (ts,sym,price,chg,vol,mcap,liq,holders,rsi,rsi_real,
            ma9,ma21,gc,score,tier,funding_rate,fr_real,chain,vm,
            vol_growth,price_compression)
           VALUES %s""",
        rows,
    )
    c.execute("DELETE FROM snapshots WHERE ts < %s", (ts - 60 * 86400,))
    db.commit()
    db.close()
    log.info(f"Snapshot: {len(rows)} tokens")
