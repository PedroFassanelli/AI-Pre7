"""Fixtures compartidas.

Regla del proyecto: **ningún test toca la red ni necesita una API key.** Los
modelos se reemplazan por `LLMFalso` (ver `dobles.py`) y la base de negocio se
siembra en un directorio temporal, así que la base real nunca se pisa.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.fuentes import db  # noqa: E402


@pytest.fixture(autouse=True)
async def base_temporal(tmp_path, monkeypatch):
    """Siembra una base descartable y apunta la configuración a ella.

    Es `autouse` a propósito: un test que se olvide de pedirla igual escribiría
    sobre `fuentes/nimbus.db`.
    """
    ruta = tmp_path / "nimbus-test.db"
    monkeypatch.setattr(config, "DB_NEGOCIO", ruta)
    await db.sembrar(ruta)
    return ruta
