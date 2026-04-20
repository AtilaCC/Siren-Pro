"""
trading/risk_manager.py — Gerenciamento de risco por trade.

Regras:
  - Risco fixo de 1% do saldo por operação
  - Limite de posição por score do sinal
  - Stop loss e take profit dinâmicos por tier
  - R/R mínimo de 1.5 enforçado antes de qualquer execução
"""

import logging

log = logging.getLogger("SIREN.risk")

# ── Parâmetros de risco ───────────────────────────────────────────────────
RISK_PER_TRADE    = 0.01     # 1% do saldo por operação
STOP_LOSS_PCT     = 0.05     # 5% de stop loss base
MAX_POSITION_USDT = 500.0    # teto absoluto em USDT por trade
MIN_POSITION_USDT = 10.0     # mínimo operável

# ── R/R por tier ──────────────────────────────────────────────────────────
# SL fixo em 5% — TP cresce com a qualidade do sinal
# S-tier (≥80): TP=15% → R/R = 3.0
# A-tier (≥65): TP=12.5% → R/R = 2.5
# B-tier (≥50): TP=10%   → R/R = 2.0
# C-tier (<50): TP=7.5%  → R/R = 1.5 (piso mínimo aceitável)
MIN_RISK_REWARD = 1.5

_TIER_TP = {
    "S": 0.15,   # score ≥ 80
    "A": 0.125,  # score ≥ 65
    "B": 0.10,   # score ≥ 50
    "C": 0.075,  # score < 50
}


def _score_to_tier(score: int) -> str:
    if score >= 80: return "S"
    if score >= 65: return "A"
    if score >= 50: return "B"
    return "C"


def validate_risk_reward(stop_pct: float, tp_pct: float) -> tuple[bool, float]:
    """
    Valida se o R/R do trade atinge o mínimo exigido.
    Retorna (aprovado, rr_calculado).
    """
    if stop_pct <= 0:
        return False, 0.0
    rr = round(tp_pct / stop_pct, 2)
    return rr >= MIN_RISK_REWARD, rr


def calc_position_size(balance_usdt: float, score: int, price: float) -> dict:
    """
    Calcula o tamanho da posição em USDT e em quantidade do ativo.

    v2: Take Profit dinâmico por tier garante R/R crescente com qualidade.
      - S-tier: TP=15% (R/R 3.0)
      - A-tier: TP=12.5% (R/R 2.5)
      - B-tier: TP=10% (R/R 2.0)
      - C-tier: TP=7.5% (R/R 1.5 — piso mínimo)

    Retorna:
      {
        "usdt":       float,
        "qty":        float,
        "stop_price": float,
        "tp_price":   float,
        "risk_usdt":  float,
        "rr":         float,   # ← novo: ratio risco/retorno real
        "tier":       str,     # ← novo: tier calculado
      }
    """
    if balance_usdt <= 0 or price <= 0:
        return _zero_position()

    tier      = _score_to_tier(score)
    tp_pct    = _TIER_TP[tier]
    sl_pct    = STOP_LOSS_PCT

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
        "rr": 0, "tier": "C",
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
