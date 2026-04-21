"""
trading/risk_manager.py — Gerenciamento de risco por trade.

Regras:
  - Risco fixo de 1% do saldo por operação
  - Limite de posição por score do sinal
  - SL dinâmico baseado em volatilidade real do token (única fonte de verdade)
  - TP por tier (cresce com a qualidade do sinal)
  - R/R mínimo de 1.5 enforçado antes de qualquer execução

v3: SL dinâmico consolidado aqui — executor.py não recalcula mais SL/TP.
"""

import logging

log = logging.getLogger("SIREN.risk")

# ── Parâmetros de risco ───────────────────────────────────────────────────
RISK_PER_TRADE    = 0.01     # 1% do saldo por operação
MAX_POSITION_USDT = 500.0    # teto absoluto em USDT por trade
MIN_POSITION_USDT = 10.0     # mínimo operável

# ── SL dinâmico — limites ─────────────────────────────────────────────────
SL_MIN_PCT = 0.03   # 3% mínimo absoluto
SL_MAX_PCT = 0.12   # 12% máximo absoluto

# ── R/R mínimo ───────────────────────────────────────────────────────────
MIN_RISK_REWARD = 1.5

# ── TP por tier — R/R cresce com qualidade do sinal ──────────────────────
# S-tier (≥80): TP=15% → R/R ≥ 1.5 mesmo com SL de 10%
# A-tier (≥65): TP=12.5%
# B-tier (≥50): TP=10%
# C-tier (<50): TP=7.5%  (piso mínimo aceitável)
_TIER_TP = {
    "S": 0.15,
    "A": 0.125,
    "B": 0.10,
    "C": 0.075,
}


def _score_to_tier(score: int) -> str:
    if score >= 80: return "S"
    if score >= 65: return "A"
    if score >= 50: return "B"
    return "C"


def calc_dynamic_sl(token: dict) -> float:
    """
    Calcula o % de Stop Loss dinamicamente com base na volatilidade real
    do token. Esta é a ÚNICA fonte de verdade para SL — executor.py usa
    este valor diretamente.

    Lógica:
      - Base: chg 24h como proxy de volatilidade
      - SL = 1.2x a volatilidade (para não ser stopado pelo ruído normal)
      - Ajustes: RSI alto → SL mais largo; volume baixo → SL mais largo
      - Fix v3: tokens com preço < $0.10 recebem SL mínimo de 5%
                (volatilidade natural de altcoins de baixa cap)
      - Limites finais: entre SL_MIN_PCT e SL_MAX_PCT

    Retorna: sl_pct como float (ex: 0.05 = 5%)
    """
    chg_abs = abs(token.get("chg", 5.0))

    if chg_abs > 30:
        sl_pct = 0.10
    elif chg_abs > 15:
        sl_pct = 0.07
    elif chg_abs > 5:
        sl_pct = 0.05
    else:
        sl_pct = 0.035

    # Ajuste RSI: sobrecomprado → SL mais largo
    rsi = token.get("rsi", 50)
    if rsi > 70:
        sl_pct += 0.015

    # Ajuste volume: liquidez baixa → spreads maiores → SL mais largo
    vol = token.get("vol", 0)
    if vol < 200_000:
        sl_pct += 0.01

    # Fix v3: altcoins de baixo preço têm volatilidade natural ≥ 5%
    # SL de 3.5% garante stop em qualquer respirada normal do mercado
    price = token.get("price", 1.0)
    if price < 0.10:
        sl_pct = max(sl_pct, 0.05)

    return max(SL_MIN_PCT, min(SL_MAX_PCT, sl_pct))


def validate_risk_reward(stop_pct: float, tp_pct: float) -> tuple[bool, float]:
    """
    Valida se o R/R do trade atinge o mínimo exigido.
    Retorna (aprovado, rr_calculado).
    """
    if stop_pct <= 0:
        return False, 0.0
    rr = round(tp_pct / stop_pct, 2)
    return rr >= MIN_RISK_REWARD, rr


def calc_position_size(balance_usdt: float, score: int, price: float,
                       token: dict = None) -> dict:
    """
    Calcula o tamanho da posição em USDT e em quantidade do ativo.

    v3: SL dinâmico consolidado — usa calc_dynamic_sl(token) como única
        fonte de verdade. TP por tier garante R/R crescente com qualidade.

    Args:
        balance_usdt: saldo disponível em USDT
        score:        score do sinal (determina tier e multiplicador)
        price:        preço atual do ativo
        token:        dict completo do token (necessário para SL dinâmico)

    Retorna dict com: usdt, qty, stop_price, tp_price, risk_usdt,
                      sl_pct, rr, tier, multiplier
    """
    if balance_usdt <= 0 or price <= 0:
        return _zero_position()

    tier   = _score_to_tier(score)
    tp_pct = _TIER_TP[tier]

    # SL dinâmico — usa token se disponível, senão fallback conservador
    if token:
        sl_pct = calc_dynamic_sl(token)
    else:
        sl_pct = 0.05  # fallback: 5% conservador

    # Validação R/R antes de calcular tamanho
    rr_ok, rr = validate_risk_reward(sl_pct, tp_pct)
    if not rr_ok:
        log.warning(f"R/R insuficiente: {rr:.2f} < {MIN_RISK_REWARD} — trade rejeitado")
        return _zero_position()

    base_usdt = balance_usdt * RISK_PER_TRADE

    # Multiplicador por tier de score
    if score >= 80:   multiplier = 1.5
    elif score >= 65: multiplier = 1.25
    elif score >= 50: multiplier = 1.0
    else:             multiplier = 0.5  # C-tier: tamanho reduzido

    usdt = min(base_usdt * multiplier, MAX_POSITION_USDT)

    if usdt < MIN_POSITION_USDT:
        log.info(f"Posição calculada ({usdt:.2f} USDT) abaixo do mínimo — ignorando")
        return _zero_position()

    qty        = usdt / price
    stop_price = round(price * (1 - sl_pct), 8)
    tp_price   = round(price * (1 + tp_pct), 8)
    risk_usdt  = round(usdt * sl_pct, 2)

    result = {
        "usdt":       round(usdt, 2),
        "qty":        round(qty, 6),
        "stop_price": stop_price,
        "tp_price":   tp_price,
        "risk_usdt":  risk_usdt,
        "sl_pct":     sl_pct,
        "multiplier": multiplier,
        "rr":         rr,
        "tier":       tier,
    }

    log.info(
        f"Posição: ${usdt:.2f} ({qty:.6f} @ ${price:.6f}) | tier={tier} "
        f"SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}% R/R={rr:.1f} | "
        f"SL=${stop_price:.6f} TP=${tp_price:.6f} | Risco=${risk_usdt:.2f}"
    )
    return result


def _zero_position() -> dict:
    return {
        "usdt": 0, "qty": 0, "stop_price": 0,
        "tp_price": 0, "risk_usdt": 0, "multiplier": 0,
        "sl_pct": 0, "rr": 0, "tier": "C",
    }


def validate_trade(balance_usdt: float, usdt_amount: float) -> tuple:
    """
    Valida se o trade pode ser executado.
    Retorna (True, '') ou (False, motivo).
    """
    if balance_usdt < MIN_POSITION_USDT:
        return False, f"Saldo insuficiente: ${balance_usdt:.2f}"
    if usdt_amount <= 0:
        return False, "Valor de posição inválido (rr ou quality gate falhou)"
    if usdt_amount > balance_usdt:
        return False, f"Posição (${usdt_amount:.2f}) > saldo (${balance_usdt:.2f})"
    return True, ""
