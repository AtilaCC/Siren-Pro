"""
trading/risk_manager.py — Gerenciamento de risco por trade.

Regras:
  - Risco fixo de 1% do saldo por operação
  - Limite de posição por score do sinal
  - Stop loss e take profit configuráveis
"""

import logging

log = logging.getLogger("SIREN.risk")

# ── Parâmetros de risco ───────────────────────────────────────────────────
RISK_PER_TRADE    = 0.01     # 1% do saldo por operação
STOP_LOSS_PCT     = 0.05     # 5% de stop loss padrão
TAKE_PROFIT_PCT   = 0.10     # 10% de take profit padrão
MAX_POSITION_USDT = 500.0    # teto absoluto em USDT por trade
MIN_POSITION_USDT = 10.0     # mínimo operável


def calc_position_size(balance_usdt: float, score: int, price: float) -> dict:
    """
    Calcula o tamanho da posição em USDT e em quantidade do ativo.

    Regras:
      - Base: 1% do saldo
      - Bônus para S-tier (+50%) e A-tier (+25%)
      - Limitado por MAX_POSITION_USDT e mínimo MIN_POSITION_USDT

    Retorna:
      {
        "usdt":       float,    # valor em USDT
        "qty":        float,    # quantidade do ativo
        "stop_price": float,    # preço de stop loss
        "tp_price":   float,    # preço de take profit
        "risk_usdt":  float,    # risco em USDT (perda máxima)
      }
    """
    if balance_usdt <= 0 or price <= 0:
        return _zero_position()

    base_usdt = balance_usdt * RISK_PER_TRADE

    # Multiplicador por tier de score
    if score >= 80:   multiplier = 1.5   # S-tier
    elif score >= 65: multiplier = 1.25  # A-tier
    elif score >= 50: multiplier = 1.0   # B-tier
    else:             multiplier = 0.5   # C-tier (muito pequeno)

    usdt = min(base_usdt * multiplier, MAX_POSITION_USDT)

    if usdt < MIN_POSITION_USDT:
        log.info(f"Posição calculada ({usdt:.2f} USDT) abaixo do mínimo — ignorando")
        return _zero_position()

    qty        = usdt / price
    stop_price = round(price * (1 - STOP_LOSS_PCT), 8)
    tp_price   = round(price * (1 + TAKE_PROFIT_PCT), 8)
    risk_usdt  = round(usdt * STOP_LOSS_PCT, 2)

    result = {
        "usdt":       round(usdt, 2),
        "qty":        round(qty, 6),
        "stop_price": stop_price,
        "tp_price":   tp_price,
        "risk_usdt":  risk_usdt,
        "multiplier": multiplier,
    }

    log.info(
        f"Posição: ${usdt:.2f} ({qty:.6f} @ ${price:.6f}) | "
        f"SL=${stop_price:.6f} TP=${tp_price:.6f} | Risco=${risk_usdt:.2f}"
    )
    return result


def _zero_position() -> dict:
    return {
        "usdt": 0, "qty": 0, "stop_price": 0,
        "tp_price": 0, "risk_usdt": 0, "multiplier": 0,
    }


def validate_trade(balance_usdt: float, usdt_amount: float) -> tuple:
    """
    Valida se o trade pode ser executado.
    Retorna (True, '') ou (False, motivo).
    """
    if balance_usdt < MIN_POSITION_USDT:
        return False, f"Saldo insuficiente: ${balance_usdt:.2f}"
    if usdt_amount <= 0:
        return False, "Valor de posição inválido"
    if usdt_amount > balance_usdt:
        return False, f"Posição (${usdt_amount:.2f}) > saldo (${balance_usdt:.2f})"
    return True, ""


def approve_trade(token: dict, balance_usdt: float = 1000.0) -> tuple:
    """
    Ponto de entrada único para aprovação de risco de um sinal.
    Chamado pelo engine.py antes de qualquer execução.

    Retorna (True, position_dict) se aprovado, ou (False, motivo_str) se bloqueado.

    Regras:
      - Score mínimo de 50 para qualquer trade
      - Posição calculada pelo calc_position_size()
      - Validação final pelo validate_trade()
    """
    sym   = token.get("sym", "?")
    score = token.get("score", 0)
    price = token.get("price", 0)

    if score < 50:
        reason = f"Score {score} abaixo do mínimo (50)"
        log.info(f"RiskEngine bloqueou ${sym}: {reason}")
        return False, reason

    if price <= 0:
        reason = "Preço inválido ou zero"
        log.info(f"RiskEngine bloqueou ${sym}: {reason}")
        return False, reason

    position = calc_position_size(balance_usdt, score, price)

    if position["usdt"] <= 0:
        reason = f"Posição calculada zerada (saldo=${balance_usdt:.2f}, score={score})"
        log.info(f"RiskEngine bloqueou ${sym}: {reason}")
        return False, reason

    ok, reason = validate_trade(balance_usdt, position["usdt"])
    if not ok:
        log.info(f"RiskEngine bloqueou ${sym}: {reason}")
        return False, reason

    log.info(
        f"RiskEngine aprovado ${sym}: "
        f"${position['usdt']:.2f} USDT | "
        f"SL={position['stop_price']:.8f} TP={position['tp_price']:.8f}"
    )
    return True, position
