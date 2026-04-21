"""
ai/claude_trade.py — Motor de decisão institucional de trading.

v2: Adicionada regra de pump legítimo com volume real.
    Caso $EDU +42% foi bloqueado incorretamente como CHAOTIC.
    Agora: chg > 25% + vol_growth > 20% + RSI < 70 → TREND, não CHAOTIC.
"""

import os
import json
import logging
import asyncio
import urllib.request

log = logging.getLogger("SIREN.claude_trade")

CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_API     = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL   = "claude-sonnet-4-20250514"


# ══════════════════════════════════════════════
# SYSTEM 1 — BASE: PRESERVAÇÃO DE CAPITAL
# ══════════════════════════════════════════════

BASE_PROMPT = """
Você é um motor de decisão quantitativo institucional especializado em trading de criptoativos na Binance Alpha.

Seu único objetivo é decidir se uma operação deve ou NÃO ser executada.

PRINCÍPIOS ABSOLUTOS:
1. Preservação de capital acima de qualquer lucro.
2. Nunca opere em mercado lateral (choppy) ou indefinido.
3. Exija confluência de: tendência + volume crescente + estrutura técnica limpa.
4. Em dúvida → NÃO ENTRE. Sempre.

PENALIZE fortemente:
- RSI > 72 (sobrecomprado — topo provável)
- Pump ocorrido SEM volume crescente (manipulação)
- Volume caindo ou baixo (< 100K USDT)
- Score inconsistente com os outros indicadores
- BTC em tendência de baixa forte

EXCEÇÃO PRIORITÁRIA — PUMP COM VOLUME REAL:
Se TODOS esses critérios forem verdadeiros simultaneamente:
  → chg_24h > 25%
  → vol_growth_pct > 20% (volume crescendo de verdade)
  → RSI < 70 (não sobrecomprado ainda)
  → score_siren >= 75
ENTÃO: classifique como TREND, nunca como CHAOTIC
Isso é momentum legítimo com volume confirmando — é oportunidade real
Confidence proporcional: vol_growth alto + RSI baixo = confidence mais alta
Exemplo: chg=42% + vol_growth=50% + RSI=54 + score=80 → TREND, conf=0.82

VALORIZE:
- Score alto (≥ 80) com múltiplos confirmadores
- Volume crescente e acima da média (vol_growth > 0)
- Tendência clara e direcional
- Setup de continuação ou pré-breakout legítimo
- RSI entre 35–65 (momentum saudável)
- Funding Rate negativo (squeeze iminente)
- Golden Cross confirmado (MA9 > MA21)

CLASSIFICAÇÃO DE REGIME OBRIGATÓRIA:
- TREND    → movimento forte, direcional, volume confirmando (inclui pumps com volume real)
- CHOPPY   → lateral, indefinido, volume fraco — NUNCA ENTRE
- REVERSAL → oversold real com sinais técnicos de recuperação
- CHAOTIC  → volatilidade sem volume real confirmando — NUNCA ENTRE

SAÍDA: responda SOMENTE JSON válido, sem markdown, sem texto extra:
{
  "trade": true ou false,
  "confidence": 0.0 a 1.0,
  "reason": "motivo objetivo em até 15 palavras",
  "regime": "TREND | CHOPPY | REVERSAL | CHAOTIC"
}
"""


# ══════════════════════════════════════════════
# SYSTEM 2 — PERFORMANCE: FILTROS DE QUALIDADE
# ══════════════════════════════════════════════

PERFORMANCE_PROMPT = """
CAMADA DE PERFORMANCE — FILTROS ADICIONAIS OBRIGATÓRIOS:

LIMITES OPERACIONAIS:
- Confidence mínima para trade: 0.75 (abaixo disso → trade: false)
- Risk/Reward mínimo implícito: 1:2 (só entre se o upside justifica)
- Máximo 4 trades simultâneos (se já há 4 abertos → trade: false)

BLOQUEIOS AUTOMÁTICOS:
- RSI ≥ 75 → BLOQUEADO (sobrecomprado)
- chg 24h > 50% SEM vol_growth → BLOQUEADO (pump exausto sem volume)
- volume_24h_usdt < 50.000 → BLOQUEADO (liquidez insuficiente)
- regime CHOPPY ou CHAOTIC → BLOQUEADO
- BTC em queda forte (btc_trend = "bear") → BLOQUEADO

ATENÇÃO — NÃO BLOQUEIE pump com volume real:
- Se chg > 25% mas vol_growth > 20% e RSI < 70 → NÃO é pump exausto
- Volume crescendo = compradores entrando = momentum válido
- Esse é exatamente o tipo de trade que queremos capturar

PRÉ-PUMP:
- Aceitar se volume estiver crescendo E confiança ≥ 0.75
- Volume flat + preço subindo = suspeito

RSI BAIXO (< 35):
- Aceitar SOMENTE se houver sinal claro de reversão (gc=true ou pre=true)
- Não comprar faca caindo

TENDÊNCIA:
- Favoreça continuação com volume, nunca exaustão sem volume
- Se MA9 < MA21 → tendência baixista → trade: false para long

COMPORTAMENTO ESPERADO:
- Conservador em mercados ambíguos e sem volume
- Agressivo em pumps com volume real confirmando
- Prefira perder oportunidade a tomar loss evitável

OBJETIVO: capturar pumps legítimos com volume, bloquear sinais falsos sem volume.
"""


# ══════════════════════════════════════════════
# PRÉ-FILTRO LOCAL (antes de chamar a IA)
# ══════════════════════════════════════════════

def _pre_filter(market_data: dict) -> tuple[bool, str]:
    """
    Filtro rápido local antes de chamar a API.
    Evita chamar a IA para casos obviamente ruins.
    Retorna (pode_continuar, motivo_bloqueio).
    """
    rsi     = market_data.get("rsi", 50)
    vol     = market_data.get("volume_24h_usdt", 0)
    chg     = market_data.get("change_24h_pct", 0)
    vg      = market_data.get("vol_growth_pct", 0)
    btc     = market_data.get("btc_trend", "neutral")

    # Bloqueios absolutos sem necessidade de IA
    if rsi >= 78:
        return False, f"RSI {rsi} sobrecomprado — bloqueado localmente"
    if vol < 50_000:
        return False, f"Volume ${vol:.0f} insuficiente — bloqueado localmente"
    if btc == "bear" and chg < 10:
        return False, "BTC bear + sinal fraco — bloqueado localmente"
    # Pump exausto: subiu muito SEM volume crescente
    if chg > 50 and vg <= 0:
        return False, f"chg={chg:.0f}% sem vol_growth — pump exausto"

    return True, ""


# ══════════════════════════════════════════════
# CHAMADA À API
# ══════════════════════════════════════════════

def _call_claude_sync(market_data: dict) -> dict:
    """Chamada síncrona à API Claude com os dois prompts combinados."""
    if not CLAUDE_API_KEY:
        log.warning("ANTHROPIC_API_KEY não configurada — bloqueando trade por segurança")
        return {
            "trade": False, "confidence": 0.0,
            "reason": "API key ausente — bloqueado por segurança",
            "regime": "UNKNOWN"
        }

    system = BASE_PROMPT.strip() + "\n\n" + PERFORMANCE_PROMPT.strip()

    user_content = f"""Analise este token e decida se deve ser operado agora.

DADOS DE MERCADO:
{json.dumps(market_data, indent=2, ensure_ascii=False)}

Responda SOMENTE com JSON válido conforme o formato especificado. Sem texto extra."""

    payload = json.dumps({
        "model":      CLAUDE_MODEL,
        "max_tokens": 150,
        "system":     system,
        "messages":   [{"role": "user", "content": user_content}],
    }).encode("utf-8")

    req = urllib.request.Request(
        CLAUDE_API,
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            raw  = data["content"][0]["text"].strip()
            raw  = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            return {
                "trade":      bool(result.get("trade", False)),
                "confidence": float(result.get("confidence", 0.0)),
                "reason":     str(result.get("reason", "sem motivo"))[:200],
                "regime":     str(result.get("regime", "UNKNOWN")),
            }
    except urllib.error.HTTPError as e:
        log.error(f"Claude API HTTP {e.code}: {e.reason}")
    except json.JSONDecodeError as e:
        log.error(f"Claude retornou JSON inválido: {e}")
    except Exception as e:
        log.error(f"Claude API falha inesperada: {e}")

    return {
        "trade": False, "confidence": 0.0,
        "reason": "erro na API — bloqueado por segurança",
        "regime": "UNKNOWN"
    }


# ══════════════════════════════════════════════
# FUNÇÃO PRINCIPAL
# ══════════════════════════════════════════════

async def claude_trade_decision(token: dict) -> dict:
    """
    Ponto de entrada principal. Chamado pelo engine ANTES de execute_signal().
    """
    from core.scoring import get_btc_context
    btc_ctx = get_btc_context()

    market_data = {
        "symbol":            token.get("sym", "?"),
        "price":             token.get("price", 0),
        "change_24h_pct":    token.get("chg", 0),
        "volume_24h_usdt":   token.get("vol", 0),
        "rsi":               token.get("rsi", 50),
        "rsi_real":          token.get("rsi_real", False),
        "score_siren":       token.get("score", 0),
        "tier":              token.get("tier", "?"),
        "vm_ratio":          token.get("vm", 0),
        "liquidity":         token.get("liq", 0),
        "holders":           token.get("holders", 0),
        "funding_rate":      token.get("fr", 0),
        "funding_real":      token.get("fr_real", False),
        "golden_cross":      token.get("gc", False),
        "pre_pump":          token.get("pre", False),
        "pre_pump_conf":     token.get("pre_conf", 0),
        "vol_growth_pct":    token.get("vol_growth", 0),
        "price_compression": token.get("price_compression", 0),
        "reversal":          token.get("rev", False),
        "chain":             token.get("chain", "?"),
        "btc_trend":         btc_ctx.get("trend", "neutral"),
        "btc_rsi":           btc_ctx.get("rsi", 50),
        "btc_chg_4h":        btc_ctx.get("chg_4h", 0),
        "btc_score_mult":    btc_ctx.get("score_mult", 1.0),
    }

    # Pré-filtro local — evita chamar API para casos óbvios
    ok, pre_reason = _pre_filter(market_data)
    if not ok:
        log.info(f"[PRÉ-FILTRO] ${token.get('sym','?')} bloqueado: {pre_reason}")
        return {
            "trade": False, "confidence": 0.0,
            "reason": pre_reason, "regime": "BLOCKED"
        }

    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: _call_claude_sync(market_data))

    # Camada de segurança: força false se confidence insuficiente
    if result["confidence"] < 0.75:
        result["trade"]  = False
        result["reason"] = f"confidence {result['confidence']:.2f} < 0.75 — bloqueado"

    # Força false em regimes proibidos
    if result["regime"] in ("CHOPPY", "CHAOTIC", "UNKNOWN", "BLOCKED"):
        result["trade"]  = False
        result["reason"] = f"regime {result['regime']} — sem operação"

    log.info(
        f"[IA] ${token.get('sym','?')} | trade={result['trade']} | "
        f"conf={result['confidence']:.2f} | regime={result['regime']} | "
        f"{result['reason']}"
    )

    return result
