"""
core/scanner.py — Busca e enriquecimento de tokens Binance Alpha + Spot.

  - fetch_alpha_tokens()  : busca tokens via API pública do Binance Alpha (RESTAURADO)
  - fetch_spot_tokens()   : fallback via /api/v3/ticker/24hr (USDT pairs)
  - fetch_klines()        : klines com fallback USDT/FDUSD
  - fetch_funding_rates() : taxas de financiamento
  - fetch_btc_context()   : contexto macro BTC
  - enrich_tokens()       : enriquece com RSI real, MA, GC, pre-pump, FR
  - build_alpha_token()   : constrói objeto de token a partir do payload Alpha
  - build_token()         : constrói objeto de token a partir do payload Spot
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
SPOT_TICKER_API = "https://api.binance.com/api/v3/ticker/24hr"
KLINES_API      = "https://api.binance.com/api/v3/klines"
FUNDING_API     = "https://fapi.binance.com/fapi/v1/premiumIndex"
BTC_KLINES      = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=50"

# ── Endpoints Alpha ─────────────────────────────────────────────────────────
ALPHA_LIST_API  = (
    "https://www.binance.com/bapi/bigdata/v1/public/bigdata/finance/exchange"
    "/listByProductId?productId=BINANCE_ALPHA&pageIndex={page}&pageSize=100"
)
ALPHA_HEADERS   = {
    "User-Agent":   "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36",
    "Accept":       "application/json",
    "Referer":      "https://www.binance.com/en/alpha",
    "lang":         "en",
}
ALPHA_MAX_PAGES    = 5      # máximo de páginas (500 tokens)
ALPHA_RETRY        = 3      # tentativas por página
ALPHA_RETRY_DELAY  = 2.0    # segundos entre tentativas
ALPHA_MIN_VOLUME   = 50_000 # volume mínimo Alpha (menor que Spot — tokens novos)

# ── Filtros de qualidade Spot ──────────────────────────────────────────────
MIN_VOLUME_24H  = 500_000    # mínimo $500k volume/dia
MIN_PRICE       = 0.000001   # filtra tokens zerados
MAX_PRICE       = 10_000     # filtra BTC/ETH de alta cap
EXCLUDE_SYMS    = {          # pares estáveis e indesejados
    "BTCUSDT","ETHUSDT","BNBUSDT","USDCUSDT","BUSDUSDT",
    "TUSDUSDT","USDTUSDT","FDUSDUSDT","DAIUSDT","EURUSDT",
    "WBTCUSDT","STETHUSDT","LDOUSDT",
}
KLINES_SEM = 20


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
# FETCH SPOT TOKENS
# ═══════════════════════════════════════

async def fetch_spot_tokens(session) -> list:
    """
    Busca todos os pares USDT da Binance Spot via /api/v3/ticker/24hr.
    Filtra por volume mínimo, preço e exclui stablecoins/BTC/ETH.
    Retorna lista de dicts no formato raw.
    """
    try:
        async with session.get(SPOT_TICKER_API, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log.error(f"Spot ticker API erro: {r.status}")
                return []
            data = await r.json()

        # Filtra apenas pares USDT com volume suficiente
        tokens = []
        for d in data:
            sym = d.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            if sym in EXCLUDE_SYMS:
                continue

            price   = float(d.get("lastPrice", 0) or 0)
            vol_usd = float(d.get("quoteVolume", 0) or 0)  # volume em USDT
            chg     = float(d.get("priceChangePercent", 0) or 0)

            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            if vol_usd < MIN_VOLUME_24H:
                continue
            if abs(chg) > 500:  # filtro anti-scam
                continue

            tokens.append(d)

        log.info(f"Spot API: {len(tokens)} tokens USDT com volume > ${MIN_VOLUME_24H:,}")
        return tokens

    except Exception as e:
        log.error(f"fetch_spot_tokens falhou: {e}")
        return []

# Mantém compatibilidade com engine.py que chama fetch_alpha_tokens
fetch_alpha_tokens = fetch_spot_tokens


# ═══════════════════════════════════════
# FETCH ALPHA TOKENS (RESTAURADO)
# ═══════════════════════════════════════

def _parse_alpha_field(d: dict, *keys, default=0.0):
    """Tenta múltiplas chaves no dict — API Alpha muda nomes entre versões."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def build_alpha_token(d: dict, weights=None) -> dict | None:
    """
    Normaliza payload da API Alpha para o formato interno do SIREN.
    A API Alpha retorna campos diferentes da Spot — mapeamento explícito aqui.
    """
    try:
        # Símbolo — Alpha usa 'symbol' ou 'tokenSymbol'
        sym = (d.get("symbol") or d.get("tokenSymbol") or "").strip().upper()
        sym = sym.replace("USDT", "").replace("FDUSD", "")
        if not sym:
            return None

        # Remove caracteres não-ASCII (tokens chineses, etc.)
        sym = sym.encode("ascii", "ignore").decode("ascii").strip()
        if not sym:
            return None

        name    = d.get("tokenName") or d.get("name") or sym

        price   = _parse_alpha_field(d, "price", "lastPrice", "currentPrice")
        chg     = _parse_alpha_field(d, "priceChangePercent", "change24h", "priceChange24h")
        vol     = _parse_alpha_field(d, "volume24h", "quoteVolume", "turnover24h")
        mcap    = _parse_alpha_field(d, "marketCap", "circulatingMarketCap", default=0)
        liq     = _parse_alpha_field(d, "liquidity", "liquidityUsd", default=0)
        holders = int(_parse_alpha_field(d, "holders", "holderCount", default=0))

        if price <= 0 or vol < ALPHA_MIN_VOLUME:
            return None
        if abs(chg) > 500:  # filtro anti-scam
            return None

        # mcap: se não vier da API, estima via volume
        if mcap <= 0:
            mcap = vol * 8

        # liquidez: se não vier, estima via volume
        if liq <= 0:
            liq = vol * 0.05

        # RSI estimado a partir da variação (será substituído pelo real no enrich)
        rsi   = max(10, min(90, round(50 + chg * 1.2)))
        vm    = min(100, round((vol / mcap) * 100)) if mcap > 0 else 0
        fr    = round(-chg * 0.003, 4)   # estimado, substituído pelo real se disponível
        score = calc_score(chg, rsi, vm, price, vol, liq, holders, fr, weights)
        tier  = "S" if score >= 80 else "A" if score >= 65 else "B" if score >= 50 else "C"

        return {
            "sym": sym, "name": name, "price": price, "chg": chg,
            "vol": vol, "mcap": mcap, "liq": liq, "holders": holders,
            "rsi": rsi, "rsi_real": False, "vm": vm, "score": score,
            "tier": tier, "gc": chg > 5 and vm > 30, "gc_real": False,
            "ma9": None, "ma21": None, "rev": chg < -15 and rsi < 35,
            "hot": False, "pre": False, "pre_conf": 0.0, "chain": "bsc",
            "fr": fr, "fr_real": False, "days": None,
            "vol_growth": 0.0, "price_compression": 0.0,
            "source": "alpha",   # marca origem para debug
        }
    except Exception:
        return None


async def fetch_alpha_tokens(session) -> list:
    """
    Busca tokens do Binance Alpha via API pública com:
      - Paginação automática (até ALPHA_MAX_PAGES páginas)
      - Retry por página (ALPHA_RETRY tentativas)
      - Fallback para Spot se Alpha indisponível
      - Filtros de qualidade Alpha-específicos

    Retorna lista de dicts no formato interno do SIREN,
    prontos para enrich_tokens() sem nenhuma mudança.
    """
    tokens_raw = []
    weights    = None  # será calculado uma vez abaixo

    for page in range(1, ALPHA_MAX_PAGES + 1):
        url         = ALPHA_LIST_API.format(page=page)
        page_data   = None

        for attempt in range(ALPHA_RETRY):
            try:
                async with session.get(
                    url,
                    headers=ALPHA_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 429:
                        log.warning(f"Alpha API rate limit — aguardando {ALPHA_RETRY_DELAY}s")
                        await asyncio.sleep(ALPHA_RETRY_DELAY)
                        continue
                    if r.status != 200:
                        log.warning(f"Alpha API página {page}: status {r.status}")
                        break
                    body = await r.json()
                    # Estrutura: {"data": {"list": [...], "total": N}}
                    data = body.get("data") or body
                    page_data = (
                        data.get("list") or
                        data.get("rows") or
                        data.get("tokens") or
                        (data if isinstance(data, list) else [])
                    )
                    break   # sucesso — sai do retry
            except asyncio.TimeoutError:
                log.warning(f"Alpha API timeout página {page} tentativa {attempt+1}")
                await asyncio.sleep(ALPHA_RETRY_DELAY)
            except Exception as e:
                log.warning(f"Alpha API erro página {page}: {e}")
                await asyncio.sleep(ALPHA_RETRY_DELAY)

        if not page_data:
            log.info(f"Alpha API: página {page} vazia — parando paginação")
            break

        tokens_raw.extend(page_data)
        log.info(f"Alpha API: página {page} — {len(page_data)} tokens")

        # Se página não veio cheia, não há mais páginas
        if len(page_data) < 100:
            break

        await asyncio.sleep(0.3)   # throttle entre páginas

    if not tokens_raw:
        log.error("Alpha API: nenhum token retornado — usando fallback Spot")
        return await fetch_spot_tokens(session)

    # Normaliza tokens para formato interno
    from core.scoring import get_adaptive_weights
    weights = get_adaptive_weights()
    tokens  = []
    for d in tokens_raw:
        t = build_alpha_token(d, weights)
        if t:
            tokens.append(t)

    log.info(f"Alpha scanner: {len(tokens_raw)} raw → {len(tokens)} tokens válidos")

    # Filtros de qualidade Alpha
    filtered = []
    seen     = set()
    for t in tokens:
        if t["sym"] in seen:
            continue
        seen.add(t["sym"])
        ok, _ = passes_quality_filter(t)
        if ok:
            filtered.append(t)

    # Ordena por score decrescente — melhores tokens primeiro
    filtered.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Alpha scanner: {len(filtered)} tokens após filtros de qualidade")
    return filtered


# ═══════════════════════════════════════
# KLINES + FUNDING
# ═══════════════════════════════════════

async def fetch_klines(session, sym, sem, interval="1h", limit=50):
    """Busca klines com fallback USDT → FDUSD."""
    async with sem:
        for suffix in ["USDT", "FDUSD"]:
            try:
                url = f"{KLINES_API}?symbol={sym}{suffix}&interval={interval}&limit={limit}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
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
    # Alpha tokens têm volume menor — usa threshold específico
    min_vol = ALPHA_MIN_VOLUME if t.get("source") == "alpha" else MIN_VOLUME_24H
    if t["vol"] < min_vol:
        return False, f"volume baixo (${t['vol']:,.0f})"
    if t["mcap"] <= 0:
        return False, "mcap inválido"
    if abs(t["chg"]) > 500:
        return False, f"variação suspeita ({t['chg']:.0f}%)"
    return True, ""


# ═══════════════════════════════════════
# PRÉ-PUMP AVANÇADO
# ═══════════════════════════════════════

def detect_pre_pump(t: dict, closes: list, volumes: list) -> tuple:
    if not closes or len(closes) < 20 or not volumes or len(volumes) < 20:
        return t.get("pre", False), 0.5

    confidence = 0.0

    vol_avg10  = sum(volumes[-10:]) / 10
    vol_rec3   = sum(volumes[-3:])  / 3
    vol_growth = (vol_rec3 - vol_avg10) / vol_avg10 if vol_avg10 > 0 else 0
    if vol_growth > 0.5:   confidence += 0.30
    elif vol_growth > 0.2: confidence += 0.15

    sma20    = sum(closes[-20:]) / 20
    std20    = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5
    bb_width = (4 * std20) / sma20 if sma20 > 0 else 0
    if bb_width < 0.04:   confidence += 0.25
    elif bb_width < 0.08: confidence += 0.12

    rsi = t.get("rsi", 50)
    if 32 <= rsi <= 48:  confidence += 0.25
    elif 48 < rsi <= 58: confidence += 0.10

    price_chg_3 = abs(closes[-1] - closes[-3]) / closes[-3] if closes[-3] > 0 else 1
    if price_chg_3 < 0.02 and vol_growth > 0.2:
        confidence += 0.20

    base_cond = -5 < t["chg"] < 12 and t["score"] >= 55 and not t.get("gc", False)
    is_pre    = base_cond and confidence >= 0.45

    t["vol_growth"]        = round(vol_growth * 100, 1)
    t["price_compression"] = round(bb_width * 100, 2)

    return is_pre, round(confidence, 2)


# ═══════════════════════════════════════
# BUILD TOKEN — FORMATO SPOT
# ═══════════════════════════════════════

def build_token(d: dict, weights=None) -> dict | None:
    """
    Constrói objeto de token normalizado a partir do payload raw da Spot API.
    Campos Spot: symbol, lastPrice, priceChangePercent, quoteVolume, count, etc.
    """
    try:
        sym_full = d.get("symbol", "")
        sym      = sym_full.replace("USDT", "").replace("FDUSD", "")
        if not sym:
            return None

        price   = float(d.get("lastPrice", 0) or 0)
        chg     = float(d.get("priceChangePercent", 0) or 0)
        vol     = float(d.get("quoteVolume", 0) or 0)   # volume em USDT
        trades  = int(d.get("count", 0) or 0)
        high24  = float(d.get("highPrice", price) or price)
        low24   = float(d.get("lowPrice", price) or price)

        # Estimar mcap e liquidez a partir do volume (sem dados reais na Spot ticker)
        # Usamos volume como proxy de liquidez
        mcap = vol * 10   # estimativa conservadora
        liq  = vol * 0.1  # proxy de liquidez

        # Holders não disponível na Spot — usa trades como proxy
        holders = min(trades, 999999)

        if price <= 0 or vol <= 0:
            return None

        rsi   = max(10, min(90, round(50 + chg * 1.2)))
        vm    = min(100, round((vol / mcap) * 100)) if mcap > 0 else 0
        fr    = round(-chg * 0.003, 4)
        score = calc_score(chg, rsi, vm, price, vol, liq, holders, fr, weights)
        tier  = "S" if score >= 80 else "A" if score >= 65 else "B" if score >= 50 else "C"

        return {
            "sym": sym, "name": sym, "price": price, "chg": chg,
            "vol": vol, "mcap": mcap, "liq": liq, "holders": holders,
            "rsi": rsi, "rsi_real": False, "vm": vm, "score": score,
            "tier": tier, "gc": chg > 5 and vm > 30, "gc_real": False,
            "ma9": None, "ma21": None, "rev": chg < -15 and rsi < 35,
            "hot": False, "pre": False, "pre_conf": 0.0, "chain": "bsc",
            "fr": fr, "fr_real": False, "days": None,
            "vol_growth": 0.0, "price_compression": 0.0,
        }
    except Exception:
        return None


# ═══════════════════════════════════════
# ENRICH TOKENS
# ═══════════════════════════════════════

async def enrich_tokens(session, tokens: list, weights=None) -> list:
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
