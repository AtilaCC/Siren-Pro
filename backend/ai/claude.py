"""
ai/claude.py — Integração com a API da Anthropic (Claude).

Preserva toda a inteligência do SIREN v6:
  - claude_analyze_token()      : análise técnica de um token
  - claude_validate_alert()     : validação de alerta antes do envio
  - claude_learn_from_results() : aprendizado de padrões em acertos/erros
  - claude_detect_narratives()  : detecção de narrativas de mercado
"""

import os
import json
import asyncio
import logging
import urllib.request

from db.connection import get_db

log = logging.getLogger("SIREN.ai")

CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_API     = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL   = "claude-sonnet-4-6"   # padrão — rápido e eficiente
CLAUDE_MODEL_OPUS = "claude-opus-4-6"  # narrativas — maior inteligência


# ═══════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════

def _fmt_price(n):
    if not n:       return "$0"
    if n >= 1000:   return f"${n:,.0f}"
    if n >= 1:      return f"${n:.4f}"
    if n >= 0.01:   return f"${n:.6f}"
    if n >= 0.0001: return f"${n:.8f}"
    return f"${n:.2e}"


def _fmt_vol(v):
    if v >= 1e9: return f"{v/1e9:.1f}B"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return f"{v:.0f}"


def _call_claude_sync(prompt: str, system: str = "", max_tokens: int = 400) -> str:
    """Chamada síncrona para a Claude API (via urllib, sem dependências extras)."""
    if not CLAUDE_API_KEY:
        return ""
    payload = json.dumps({
        "model":      CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system":     system or "Você é um analista quantitativo de crypto especializado em Binance Alpha.",
        "messages":   [{"role": "user", "content": prompt}],
    }).encode()
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
            return data["content"][0]["text"].strip()
    except Exception as e:
        log.warning(f"Claude API falha: {e}")
        return ""


def _save_claude_analysis(sym: str, atype: str, verdict: str, reasoning: str, raw: str):
    """Persiste análise Claude no banco."""
    try:
        db = get_db()
        db.cursor().execute(
            """INSERT INTO claude_analyses (ts, sym, analysis_type, verdict, reasoning, raw_response)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (__import__("time").time().__int__(), sym, atype, verdict, reasoning, raw[:1000]),
        )
        db.commit()
        db.close()
    except Exception as e:
        log.warning(f"_save_claude_analysis falhou: {e}")


# ═══════════════════════════════════════
# ANÁLISE DE TOKEN
# ═══════════════════════════════════════

async def claude_analyze_token(token: dict) -> dict:
    """
    Análise técnica institucional de um token.
    Retorna dict com: verdict, confidence, reasoning, risk.
    """
    if not CLAUDE_API_KEY:
        return {"verdict": "SKIP", "confidence": 0, "reasoning": "API key não configurada"}

    # Importa get_btc_context aqui para evitar circular import
    from core.scoring import get_btc_context
    btc_ctx = get_btc_context()

    fr_txt = (
        f"Funding Rate REAL: {token['fr']:+.4f}% "
        f"{'← SHORT SQUEEZE IMINENTE' if token['fr'] < -0.05 else '← pressão vendedora' if token['fr'] > 0.05 else ''}"
        if token.get("fr_real") else "FR: não disponível em futuros"
    )
    gc_txt = (
        f"Golden Cross (MA9>MA21): {'✅ CONFIRMADO' if token['gc'] else '❌ não ativo'}"
        if token.get("gc_real") else "Golden Cross: dados insuficientes"
    )
    pre_txt = (
        f"⚠ Pré-pump detectado com {token.get('pre_conf',0)*100:.0f}% de confiança"
        if token.get("pre") else "Pré-pump: não detectado"
    )
    btc_txt  = (
        f"BTC tendência macro: {btc_ctx.get('trend','?').upper()} | "
        f"{btc_ctx.get('chg_4h',0):+.1f}% nas últimas 4h | RSI BTC: {btc_ctx.get('rsi',50):.0f}"
    )
    vol_txt  = f"Crescimento de volume (últimas 3 barras vs média 10): {token.get('vol_growth', 0):+.0f}%"
    bb_txt   = (
        f"Bollinger Bandwidth (compressão): {token.get('price_compression', 0):.2f}%"
        f" {'← SQUEEZE ATIVO' if token.get('price_compression', 0) < 4 else ''}"
    )
    days_txt = f"Dias na Binance Alpha: {token.get('days','?')}" if token.get("days") else ""

    system = (
        "Você é um analista quantitativo sênior especializado em Binance Alpha tokens. "
        "Seu trabalho é identificar oportunidades reais de trading com base em dados técnicos. "
        "Seja objetivo, direto e cético — rejeite sinais fracos ou manipulados. "
        "Seu histórico de acertos é monitorado: só aprove alertas com alta convicção."
    )

    prompt = f"""Analise este token Binance Alpha com precisão institucional.

╔══════════════════════════════════╗
  TOKEN: ${token['sym']}  ({token.get('chain','?').upper()})
╚══════════════════════════════════╝

PREÇO E MOMENTUM
  Preço atual:    {_fmt_price(token['price'])}
  Variação 24h:   {token['chg']:+.1f}%
  Volume 24h:     ${_fmt_vol(token['vol'])}
  Liquidez:       ${_fmt_vol(token['liq'])}
  Vol/MCap (VM):  {token['vm']}%

INDICADORES TÉCNICOS
  RSI{'✓ REAL' if token['rsi_real'] else '~ estimado'}: {token['rsi']}
  {gc_txt}
  {fr_txt}
  {vol_txt}
  {bb_txt}

FUNDAMENTAIS
  Holders: {token['holders']:,}
  Score SIREN: {token['score']}/99 ({token['tier']}-Tier)
  {days_txt}

CONTEXTO MACRO
  {btc_txt}

SINAIS COMPOSTOS
  {pre_txt}
  Reversão detectada: {'SIM (RSI<35 + queda>15%)' if token.get('rev') else 'não'}

CRITÉRIO DE CLASSIFICAÇÃO
Responda com um destes vereditos (sem invenção):
  PUMP_FORTE   → múltiplos confirmadores, alta convicção, volume real
  POSSIVEL_PUMP → sinal razoável mas com 1-2 incertezas
  FAKE_PUMP    → FOMO, pump já aconteceu, manipulação, holders baixo
  REVERSAO     → oversold real com sinais técnicos de recuperação
  IGNORAR      → dados fracos, risco alto, sem edge claro

Responda SOMENTE em JSON puro, sem markdown, sem explicação extra:
{{"verdict":"PUMP_FORTE","confidence":82,"reasoning":"MA9>MA21 confirmado + RSI 28 oversold + funding -0.07% squeeze iminente","risk":"holder count baixo pode limitar upside"}}"""

    loop = asyncio.get_running_loop()
    raw  = await loop.run_in_executor(None, lambda: _call_claude_sync(prompt, system=system, max_tokens=250))

    try:
        result = json.loads(raw)
        _save_claude_analysis(token["sym"], "market_analysis", result.get("verdict", "?"), result.get("reasoning", ""), raw)
        return result
    except Exception:
        return {"verdict": "SKIP", "confidence": 0, "reasoning": raw[:100] if raw else "parse error"}


# ═══════════════════════════════════════
# VALIDAÇÃO DE ALERTA
# ═══════════════════════════════════════

async def claude_validate_alert(token: dict, alert_type: str, priority: int) -> bool:
    """
    Valida um alerta antes do envio.
    Só consulta a IA para alertas de alta prioridade (>= 7).
    """
    if not CLAUDE_API_KEY or priority < 7:
        return True

    from core.scoring import get_btc_context
    btc_ctx = get_btc_context()

    prompt = f"""Valide este alerta de trading antes de enviar ao Telegram.

ALERTA: {alert_type} — ${token['sym']}
Score: {token['score']} | RSI: {token['rsi']} | Chg: {token['chg']:+.1f}%
FR: {token['fr']:+.4f}% | VM: {token['vm']}% | Prioridade: {priority}/10
BTC: {btc_ctx.get('trend','?')}

Este alerta deve ser enviado? Critérios: não é FOMO, tem confirmação técnica, não é ruído.
Responda SOMENTE: {{"send":true,"reason":"motivo breve"}} ou {{"send":false,"reason":"motivo"}}"""

    loop = asyncio.get_running_loop()
    raw  = await loop.run_in_executor(None, lambda: _call_claude_sync(prompt, max_tokens=100))
    try:
        result = json.loads(raw)
        send   = result.get("send", True)
        reason = result.get("reason", "")
        if not send:
            log.info(f"Claude bloqueou alerta {alert_type} ${token['sym']}: {reason}")
        return send
    except Exception:
        return True


# ═══════════════════════════════════════
# APRENDIZADO COM RESULTADOS
# ═══════════════════════════════════════

async def claude_learn_from_results(verified_results: list):
    """Analisa batch de acertos/erros e retorna insights para melhoria do sistema."""
    if not CLAUDE_API_KEY or len(verified_results) < 5:
        return

    hits   = [r for r in verified_results if r["hit"]]
    misses = [r for r in verified_results if not r["hit"]]
    avg_hit_pct  = sum(r["pct"] for r in hits)  / len(hits)  if hits  else 0
    avg_miss_pct = sum(r["pct"] for r in misses) / len(misses) if misses else 0

    def fmt_samples(lst, n=4):
        return "\n".join(
            f"  ${r['sym']} | {r['label']} | {r['pct']:+.1f}% | RSI:{r.get('rsi','?')} Score:{r.get('score','?')}"
            for r in lst[:n]
        )

    system = (
        "Você é um pesquisador quantitativo analisando performance de um sistema de alertas de trading. "
        "Seu objetivo é identificar padrões nos acertos e erros e sugerir ajustes concretos nos parâmetros. "
        "Seja analítico, preciso e objetivo. Foque em dados, não em especulação."
    )

    prompt = f"""Analise a performance deste lote de alertas verificados após 24h.

RESUMO DO LOTE
  Total: {len(verified_results)} alertas
  Acertos: {len(hits)} ({len(hits)/len(verified_results)*100:.0f}%)
  Erros:   {len(misses)}
  Retorno médio (acertos): {avg_hit_pct:+.1f}%
  Retorno médio (erros):   {avg_miss_pct:+.1f}%

AMOSTRAS — ACERTOS:
{fmt_samples(hits)}

AMOSTRAS — ERROS:
{fmt_samples(misses)}

TAREFA
Identifique padrões e responda em JSON puro:
{{
  "pattern_wins": "o que os acertos têm em comum (RSI, score, tipo, chain...)",
  "pattern_losses": "por que os erros falharam (FOMO, dados fracos, BTC bearish...)",
  "suggestion": "ajuste mais importante a fazer no sistema",
  "rsi_threshold": 35,
  "score_threshold": 68,
  "avoid_label": "tipo de alerta com pior performance ou null",
  "confidence": 75
}}"""

    loop = asyncio.get_running_loop()
    raw  = await loop.run_in_executor(None, lambda: _call_claude_sync(prompt, system=system, max_tokens=350))
    try:
        insights = json.loads(raw)
        _save_claude_analysis("*", "learning", "INSIGHTS", str(insights), raw)
        log.info(
            f"Claude learning: {insights.get('suggestion','')} | "
            f"RSI≥{insights.get('rsi_threshold')} Score≥{insights.get('score_threshold')}"
        )
        return insights
    except Exception:
        log.info(f"Claude learning raw: {raw[:200]}")
        return None


# ═══════════════════════════════════════
# DETECÇÃO DE NARRATIVAS
# ═══════════════════════════════════════

async def claude_detect_narratives(tokens: list) -> dict:
    """
    Detecta narrativas de mercado dominantes no conjunto de tokens.
    Usa Claude Opus 4 — maior capacidade de identificar padrões macro complexos.
    Roda a cada 4 ciclos, então o custo extra é justificado.
    """
    if not CLAUDE_API_KEY or len(tokens) < 10:
        return {}

    top20      = sorted(tokens, key=lambda t: t["score"], reverse=True)[:20]
    token_list = "\n".join([
        f"${t['sym']} | score:{t['score']} | chg:{t['chg']:+.1f}% | "
        f"chain:{t['chain']} | rsi:{t.get('rsi','?')} | vol:${t.get('vol',0)/1e6:.1f}M"
        for t in top20
    ])

    system = (
        "Você é um analista macro sênior de criptoativos com visão institucional. "
        "Identifica narrativas emergentes, rotações de capital entre setores e "
        "padrões de comportamento de mercado antes que se tornem óbvios. "
        "Seja preciso, perspicaz e objetivo."
    )

    prompt = f"""Analise estes top tokens Binance Alpha e identifique narrativas de mercado ativas.

TOKENS (top 20 por score):
{token_list}

Tarefas:
1. Identifique a narrativa temática dominante (AI, memecoins, DeFi, L2, gaming, RWA, etc.)
2. Qual blockchain está recebendo mais fluxo?
3. Há rotação setorial em andamento?
4. Qual o insight mais importante para traders agora?

Responda SOMENTE em JSON:
{{"dominant_narrative":"nome","tokens_in_narrative":["SYM1","SYM2"],"hot_chain":"ethereum","sector_rotation":"de X para Y ou null","insight":"observação estratégica em 1 frase","confidence":75}}"""

    # Chamada com Opus — modelo mais capaz para análise macro
    def _call_opus():
        if not CLAUDE_API_KEY:
            return ""
        payload = json.dumps({
            "model":      CLAUDE_MODEL_OPUS,
            "max_tokens": 350,
            "system":     system,
            "messages":   [{"role": "user", "content": prompt}],
        }).encode()
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
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data["content"][0]["text"].strip()
        except Exception as e:
            log.warning(f"Claude Opus narrativas falha: {e}")
            return ""

    loop = asyncio.get_running_loop()
    raw  = await loop.run_in_executor(None, _call_opus)
    try:
        raw    = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        _save_claude_analysis(
            "*", "narrative",
            result.get("dominant_narrative", "?"),
            result.get("insight", ""),
            raw
        )
        log.info(
            f"[OPUS] Narrativa: {result.get('dominant_narrative')} | "
            f"chain: {result.get('hot_chain')} | "
            f"conf: {result.get('confidence')}%"
        )
        return result
    except Exception:
        return {}


# ═══════════════════════════════════════
# ANÁLISE GENÉRICA (para a API REST)
# ═══════════════════════════════════════

def analyze_text(text: str, system: str = "") -> str:
    """
    Função genérica para POST /analyze da API REST.
    Recebe texto livre e retorna análise da IA.
    """
    return _call_claude_sync(text, system=system, max_tokens=500)
