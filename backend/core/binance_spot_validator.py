# backend/core/binance_validator.py
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
import time
from typing import Set, Optional

logger = logging.getLogger(_name_)

class BinanceSpotValidator:
    """
    Validador de símbolos para Binance Spot only.
    Evita os erros constantes de "Invalid symbol" (-1121).
    """

    def _init_(self, client: Client):
        self.client = client
        self.valid_spot_usdt_pairs: Set[str] = set()
        self.last_update: float = 0
        self.update_interval: int = 1800  # 30 minutos

    def update_valid_pairs(self) -> Set[str]:
        """Atualiza a lista de pares USDT válidos no Spot"""
        now = time.time()
        if now - self.last_update < self.update_interval and self.valid_spot_usdt_pairs:
            return self.valid_spot_usdt_pairs

        try:
            exchange_info = self.client.get_exchange_info()
            self.valid_spot_usdt_pairs = {
                s['symbol'] for s in exchange_info['symbols']
                if (s.get('status') == 'TRADING' and
                    s.get('quoteAsset') == 'USDT' and
                    s.get('isSpotTradingAllowed', True))
            }
            self.last_update = now
            logger.info(f"✅ BinanceSpotValidator: {len(self.valid_spot_usdt_pairs)} pares USDT Spot carregados com sucesso.")
            return self.valid_spot_usdt_pairs
        except Exception as e:
            logger.error(f"Erro ao carregar pares Spot da Binance: {e}")
            return self.valid_spot_usdt_pairs  # mantém o que já tinha

    def is_valid_symbol(self, symbol: str) -> bool:
        """Retorna True se o símbolo é válido no Spot"""
        clean = symbol.replace('$', '').upper().strip()
        if not clean.endswith('USDT'):
            clean += 'USDT'
        return clean in self.valid_spot_usdt_pairs

    def get_clean_symbol(self, symbol: str) -> Optional[str]:
        """Retorna o símbolo limpo (ex: 'POPUSDT') ou None se inválido"""
        if not self.is_valid_symbol(symbol):
            return None
        clean = symbol.replace('$', '').upper().strip()
        if not clean.endswith('USDT'):
            clean += 'USDT'
        return clean