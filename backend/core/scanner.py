"""
core/scanner.py — Busca e enriquecimento de tokens Binance SPOT.

Troca Binance Alpha por Binance Spot:
  - fetch_spot_tokens()   : busca tokens via /api/v3/ticker/24hr (USDT pairs)
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
SPOT_TICKER_API = "https://api.binance.com/api/v3/ticker/24hr"
KLINES_API      = "https://api.binance.com/api/v3/klines"
FUNDING_API     = "https://fapi.binance.com/fapi/v1/premiumIndex"
BTC_KLINES      = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=50"

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
# VOLUME RELATIVO — ANTECIPAÇÃO DE MOVIMENTO
# ═══════════════════════════════════════

def calc_relative_volume(sym: str, current_vol: float) -> dict:
    """
    Compara o volume atual com a média histórica dos últimos 7 dias.

    Volume Relativo (RVol) = volume_atual / media_7d

    Interpretação:
      RVol > 3.0 → spike extremo — movimento iminente ou em curso
      RVol > 2.0 → acumulação significativa — sinal de antecipação
      RVol > 1.5 → volume acima da média — interesse crescente
      RVol < 0.8 → volume fraco — evitar entrada
    """
    try:
        db = get_db()
        c  = db.cursor()
        week_ago = int(time.time()) - 7 * 86400
        c.execute(
            """SELECT AVG(vol), COUNT(*), MAX(vol)
               FROM snapshots
               WHERE sym=%s AND ts >= %s AND vol > 0""",
            (sym, week_ago),
        )
        row = c.fetchone()
        db.close()

        if not row or not row[0] or row[1] < 10:
            return {"rvol": 1.0, "avg_7d": current_vol, "spike": False, "antecipacao": False, "rvol_label": "SEM_HISTORICO"}

        avg_7d = float(row[0])
        max_7d = float(row[2])
        if avg_7d <= 0:
            return {"rvol": 1.0, "avg_7d": 0, "spike": False, "antecipacao": False, "rvol_label": "SEM_HISTORICO"}

        rvol = round(current_vol / avg_7d, 2)

        if rvol >= 3.0:   label = "SPIKE_EXTREMO"
        elif rvol >= 2.0: label = "ACUMULACAO"
        elif rvol >= 1.5: label = "ACIMA_MEDIA"
        elif rvol >= 0.8: label = "NORMAL"
        else:             label = "FRACO"

        return {
            "rvol":        rvol,
            "avg_7d":      round(avg_7d, 2),
            "max_7d":      round(max_7d, 2),
            "spike":       rvol >= 3.0,
            "antecipacao": rvol >= 2.0,
            "rvol_label":  label,
        }
    except Exception as e:
        log.warning(f"calc_relative_volume falhou para {sym}: {e}")
        return {"rvol": 1.0, "avg_7d": current_vol, "spike": False, "antecipacao": False, "rvol_label": "ERRO"}


def enrich_with_rvol(tokens: list) -> list:
    """
    Enriquece lista de tokens com volume relativo.
    Chamado após enrich_tokens() no ciclo principal.
    Tokens com spike de volume sobem no score.
    """
    spike_count = antecipacao_count = 0
    for t in tokens:
        rv = calc_relative_volume(t["sym"], t["vol"])
        t["rvol"]        = rv["rvol"]
        t["rvol_label"]  = rv["rvol_label"]
        t["vol_spike"]   = rv["spike"]
        t["vol_antecip"] = rv["antecipacao"]
        t["vol_avg_7d"]  = rv["avg_7d"]

        if rv["spike"]:
            bonus = min(8, round((rv["rvol"] - 3.0) * 2 + 6))
            t["score"] = min(99, t["score"] + bonus)
            spike_count += 1
            log.info(
                f"[RVOL SPIKE] ${t['sym']} | RVol={rv['rvol']}x | "
                f"vol=${t['vol']/1e6:.1f}M vs avg=${rv['avg_7d']/1e6:.1f}M | "
                f"score +{bonus} → {t['score']}"
            )
        elif rv["antecipacao"]:
            t["score"] = min(99, t["score"] + 3)
            antecipacao_count += 1

        t["tier"] = "S" if t["score"] >= 80 else "A" if t["score"] >= 65 else "B" if t["score"] >= 50 else "C"

    if spike_count or antecipacao_count:
        log.info(f"RVol: {spike_count} spikes | {antecipacao_count} antecipações detectadas")
    return tokens



# ═══════════════════════════════════════
# FILTRO ANTI-SCAM
# ═══════════════════════════════════════

def passes_quality_filter(t: dict) -> tuple:
    """Retorna (True, '') ou (False, motivo)."""
    if t["vol"] < MIN_VOLUME_24H:
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
            "rvol": 1.0, "rvol_label": "SEM_HISTORICO", "vol_spike": False, "vol_antecip": False, "vol_avg_7d": 0.0,
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

    # Volume relativo — antecipação de movimento
    tokens = enrich_with_rvol(tokens)

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
