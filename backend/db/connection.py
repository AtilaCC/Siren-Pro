"""
db/connection.py — Gerenciamento de conexão PostgreSQL
"""

import os
import time
import logging
import psycopg2
import psycopg2.extras
from psycopg2 import OperationalError

log = logging.getLogger("SIREN.db")

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_db():
    """
    Retorna uma conexão psycopg2 usando DATABASE_URL.
    Tenta reconectar até 3 vezes em caso de falha.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL não configurada. Defina a variável de ambiente."
        )

    for attempt in range(3):
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            conn.autocommit = False
            return conn
        except OperationalError as e:
            log.warning(f"DB conexão falhou (tentativa {attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise OperationalError(
        "Não foi possível conectar ao PostgreSQL após 3 tentativas."
    )
