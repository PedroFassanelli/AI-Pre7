"""Configuración compartida: orquestador, Redis, worker y observabilidad.

Un solo lugar donde se leen las variables de entorno, se resuelven las rutas y
se construyen los modelos. El resto de los módulos importan de acá y no vuelven
a tocar ``os.environ``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final, Literal

from dotenv import load_dotenv

load_dotenv()

# --- Rutas ------------------------------------------------------------------

BASE_DIR: Final[Path] = Path(__file__).resolve().parent

#: Documentación de `nimbus-sdk`: el mismo corpus de la Pre-entrega 4.
DIR_DOCS: Final[Path] = BASE_DIR / "fuentes" / "docs"

#: Base de comercios y pagos: la misma de la Pre-entrega 5. El worker la
#: siembra al arrancar si no existe. En Docker vive en un volumen (`/data`).
DB_NEGOCIO: Path = Path(
    os.getenv("DB_NEGOCIO", "").strip() or BASE_DIR / "fuentes" / "nimbus.db"
)


# --- LLM --------------------------------------------------------------------

Proveedor = Literal["openai", "anthropic"]

PROVEEDOR: Final[Proveedor] = os.getenv("PROVEEDOR", "openai").strip().lower()  # type: ignore[assignment]

#: Modelo por proveedor cuando `MODELO` no está seteado.
MODELOS_POR_DEFECTO: Final[dict[str, str]] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}

MODELO: Final[str] = os.getenv("MODELO", "").strip() or MODELOS_POR_DEFECTO.get(
    PROVEEDOR, "gpt-4o-mini"
)

#: El supervisor puede usar un modelo distinto al de los especialistas. Es el
#: único rol que decide flujo, así que es donde más rinde un modelo mejor.
MODELO_SUPERVISOR: Final[str] = os.getenv("MODELO_SUPERVISOR", "").strip() or MODELO

TEMPERATURA: Final[float] = float(os.getenv("TEMPERATURA", "0"))


# --- Límites: la defensa contra el "supervisor infinito" --------------------

#: Cuántas veces puede decidir el supervisor en una misma tarea. Al llegar al
#: techo, la próxima decisión se reemplaza por "ir a síntesis", diga lo que
#: diga el modelo.
MAX_PASOS_SUPERVISOR: Final[int] = int(os.getenv("MAX_PASOS_SUPERVISOR", "6"))

#: Cuántas veces puede intervenir cada especialista. La primera es el trabajo;
#: la segunda, una corrección. Una tercera casi nunca arregla lo que no
#: arreglaron las dos anteriores.
MAX_RONDAS_POR_AGENTE: Final[int] = int(os.getenv("MAX_RONDAS_POR_AGENTE", "2"))

#: Techo de super-pasos del ciclo ReAct interno de cada especialista.
LIMITE_RECURSION_ESPECIALISTA: Final[int] = int(
    os.getenv("LIMITE_RECURSION_ESPECIALISTA", "12")
)


def limite_recursion_grafo(max_pasos: int | None = None) -> int:
    """`recursion_limit` del grafo principal, derivado de los otros límites.

    Cada decisión del supervisor puede ir seguida de un especialista (2
    super-pasos por vuelta), más la síntesis final. Con ese margen, el
    `recursion_limit` de LangGraph queda como **última** barrera: si alguna
    vez salta, es que las guardas de `agents/supervisor.py` tienen un bug.

    Se lee `MAX_PASOS_SUPERVISOR` en cada llamada (no como default del
    argumento) para que un cambio de configuración en runtime lo acompañe.
    """
    pasos = max_pasos if max_pasos is not None else MAX_PASOS_SUPERVISOR
    return 2 * pasos + 4


# --- Human-in-the-loop -------------------------------------------------------
#
# Toda acción con efectos secundarios requiere aprobación (ver `app/hitl.py`).
# Estos umbrales no deciden *si* se pide aprobación, sino qué razones extra se
# le muestran a quien aprueba.

UMBRAL_MONTO_ALTO_CENTAVOS: Final[int] = int(os.getenv("UMBRAL_MONTO_ALTO_CENTAVOS", "1000000"))
UMBRAL_PAGOS_MASIVO: Final[int] = int(os.getenv("UMBRAL_PAGOS_MASIVO", "3"))


# --- Redis, cola y worker ----------------------------------------------------

REDIS_URL: Final[str] = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()

#: Prefijo de las claves de los jobs (`nimbus:job:<id>`). Los checkpoints de
#: LangGraph usan los prefijos propios de `langgraph-checkpoint-redis`.
PREFIJO_REDIS: Final[str] = os.getenv("PREFIJO_REDIS", "nimbus").strip()

#: Cuánto vive un job (y sus checkpoints) en Redis después de su última
#: actualización. Alcanza para consultar el resultado; no es un archivo.
TTL_JOB_SEGUNDOS: Final[int] = int(os.getenv("TTL_JOB_SEGUNDOS", str(24 * 3600)))

#: Tiempo máximo de una corrida del grafo dentro del worker. Pasado ese tiempo,
#: Arq cancela la tarea y el job queda en FAILED.
TIMEOUT_TAREA_SEGUNDOS: Final[int] = int(os.getenv("TIMEOUT_TAREA_SEGUNDOS", "300"))

#: Cuántas tareas corre a la vez cada proceso worker. Son corutinas: casi todo
#: el tiempo es espera de red al proveedor del LLM.
MAX_TAREAS_CONCURRENTES: Final[int] = int(os.getenv("MAX_TAREAS_CONCURRENTES", "10"))

#: Si un job RUNNING no recibe ningún latido en este tiempo, se considera
#: huérfano (el worker murió) y se pasa a FAILED. Es la red de seguridad contra
#: el cliente que hace polling para siempre.
SIN_LATIDO_SEGUNDOS: Final[int] = int(
    os.getenv("SIN_LATIDO_SEGUNDOS", str(TIMEOUT_TAREA_SEGUNDOS + 60))
)


# --- Observabilidad (Arize Phoenix) -------------------------------------------

#: `0` apaga la instrumentación (los tests corren así).
TRAZAS_ACTIVAS: Final[bool] = os.getenv("TRAZAS_ACTIVAS", "1").strip() not in {"0", "false", ""}

#: Endpoint OTLP de Phoenix. HTTP: `http://<host>:6006/v1/traces`.
PHOENIX_COLLECTOR_ENDPOINT: Final[str] = os.getenv(
    "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006/v1/traces"
).strip()

#: Proyecto de Phoenix donde caen las trazas.
PHOENIX_PROJECT_NAME: Final[str] = os.getenv("PHOENIX_PROJECT_NAME", "nimbus-api").strip()


# --- Construcción de los modelos --------------------------------------------


class ConfiguracionInvalida(RuntimeError):
    """La configuración no alcanza para construir el LLM."""


def construir_llm(modelo: str | None = None):  # -> BaseChatModel
    """Devuelve el chat model del proveedor configurado.

    El import es perezoso a propósito: quien use `PROVEEDOR=openai` no debería
    necesitar `langchain-anthropic` instalado, ni al revés.
    """
    nombre = modelo or MODELO
    if PROVEEDOR == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise ConfiguracionInvalida(
                "Falta OPENAI_API_KEY. Copiá .env.example a .env y completala, "
                "o usá PROVEEDOR=anthropic."
            )
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=nombre, temperature=TEMPERATURA)

    if PROVEEDOR == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ConfiguracionInvalida(
                "Falta ANTHROPIC_API_KEY. Copiá .env.example a .env y completala, "
                "o usá PROVEEDOR=openai."
            )
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=nombre, temperature=TEMPERATURA)

    raise ConfiguracionInvalida(
        f"PROVEEDOR={PROVEEDOR!r} no es válido. Usá 'openai' o 'anthropic'."
    )


# --- Consola ----------------------------------------------------------------


def configurar_consola() -> None:
    """Fuerza UTF-8 en stdout/stderr (en Windows la consola usa ANSI)."""
    for flujo in (sys.stdout, sys.stderr):
        reconfigurar = getattr(flujo, "reconfigure", None)
        if reconfigurar is not None:
            try:
                reconfigurar(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - consolas exóticas
                pass
