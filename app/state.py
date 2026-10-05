"""Esquema del estado compartido del orquestador.

El estado es la única vía de comunicación entre nodos: ningún agente le habla a
otro directamente. Por eso su forma decide dos cosas que la consigna pide
explícitamente:

1. **Trazabilidad**: cada aporte queda registrado como una `Contribucion` con
   el nombre del agente, la ronda, la instrucción que recibió y la evidencia
   cruda que produjo. Siempre se puede responder "¿quién dijo esto y a partir
   de qué dato?".
2. **Sin pérdida de contexto**: los campos que acumulan (`contribuciones`,
   `decisiones`, `pasos_supervisor`) usan un reducer. Un nodo devuelve solo su
   aporte y LangGraph lo concatena; ningún nodo puede pisar por accidente lo
   que escribió otro.

Los campos que se **reemplazan** (`next_agent`, `instruccion_actual`,
`task_completed`, `respuesta_final`) son, a propósito, los que tienen un único
escritor: el supervisor los tres primeros, el sintetizador el último.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import MessagesState

# --- Nombres de nodos -------------------------------------------------------
#
# Son `Literal` y no `str` para que un typo ("investigadr") sea un error del
# chequeo de tipos y no un KeyError en tiempo de ejecución, en el medio de una
# corrida que ya gastó tokens.

Especialista = Literal["investigador", "analista"]

#: A dónde puede mandar el supervisor. `sintetizador` es la fase final: una vez
#: ahí, el grafo no vuelve atrás.
Destino = Literal["investigador", "analista", "sintetizador"]

ESPECIALISTAS: tuple[Especialista, ...] = ("investigador", "analista")


class Evidencia(TypedDict):
    """Una llamada a herramienta hecha por un especialista, con su resultado."""

    herramienta: str
    argumentos: dict[str, Any]
    resultado: Any


class Contribucion(TypedDict):
    """Lo que aportó un especialista en una intervención.

    `contenido` es el texto que escribió el agente; `evidencia` es lo que
    devolvieron sus herramientas. Se guardan los dos porque sirven a lectores
    distintos: el supervisor evalúa el `contenido`, el analista recibe la
    `evidencia` del investigador, y los chequeos automáticos comparan uno
    contra otra.
    """

    agente: Especialista
    #: 1 para la primera intervención de ese agente, 2 para la corrección, etc.
    ronda: int
    instruccion: str
    contenido: str
    evidencia: list[Evidencia]
    #: Hallazgos **bloqueantes** de los chequeos deterministas (ver
    #: `agents/base.py`): mientras la última ronda de un agente tenga alguno,
    #: el criterio `respaldo` de la rúbrica no se cumple.
    problemas: list[str]
    #: Hallazgos **no bloqueantes**: algo que el agente hizo fuera de su rol y
    #: que el resto del sistema descarta (p. ej. cifras escritas por el
    #: investigador, que no se usan porque los números son del analista).
    avisos: list[str]


class Decision(TypedDict):
    """Una decisión del supervisor, tal como quedó después de las guardas."""

    paso: int
    #: Lo que propuso el modelo (`finalizar` incluido).
    propuesta: str
    #: A dónde se fue efectivamente. Difiere de `propuesta` cuando actuó una guarda.
    destino: Destino
    instruccion: str
    motivo: str
    #: Resultado de la rúbrica: criterio -> cumple.
    rubrica: dict[str, bool]
    #: Si una guarda determinista pisó la propuesta del modelo, cuál y por qué.
    guarda: str | None


class AccionPropuesta(TypedDict):
    """Una acción con efecto secundario, ya validada contra la evidencia.

    La propone el modelo (`planificador`), pero los `pago_ids` y el monto los
    fija el código: solo sobreviven pagos que el investigador efectivamente
    consultó y que figuran como fallidos.
    """

    tipo: Literal["programar_reintentos"]
    comercio_id: int
    comercio: str
    pago_ids: list[str]
    monto_total_centavos: int
    moneda: str
    codigos_error: list[str]
    motivo: str
    #: Por qué el sistema la clasificó como crítica (ver `app/hitl.py`).
    criticidad: list[str]
    #: Pagos que propuso el modelo y el código descartó, con el motivo.
    descartados: list[str]


class Aprobacion(TypedDict):
    """La respuesta humana que reanuda el grafo."""

    aprobado: bool
    revisor: str
    comentario: str | None
    decidido_en: str


class ResultadoAccion(TypedDict):
    ejecutada: bool
    detalle: str
    pago_ids: list[str]


class EstadoOrquestador(MessagesState):
    """Estado del grafo.

    Hereda de `MessagesState`, que aporta `messages` con el reducer
    `add_messages`. Ahí queda la conversación **legible**: la pregunta, un
    mensaje por especialista (con `name=` del agente) y la respuesta final.
    Lo que los agentes necesitan para trabajar no viaja por `messages` sino por
    `contribuciones`: ver "Contaminación de contexto" en el README.
    """

    # --- Control de flujo (escribe solo el supervisor) ----------------------

    #: Próximo nodo. Lo lee la arista condicional del supervisor.
    next_agent: Destino
    #: Instrucción concreta para el próximo especialista.
    instruccion_actual: str

    # --- Registro (se acumula) ----------------------------------------------

    contribuciones: Annotated[list[Contribucion], operator.add]
    decisiones: Annotated[list[Decision], operator.add]
    #: Contador de pasos del supervisor. Es la condición de parada dura.
    pasos_supervisor: Annotated[int, operator.add]

    # --- Cierre ---------------------------------------------------------------

    #: Lo escribe el supervisor: `True` solo si cerró por decisión propia con la
    #: rúbrica completa, `False` si el cierre lo forzó una guarda.
    task_completed: bool
    #: Lo escribe el sintetizador (y lo completa `ejecutar_accion`).
    respuesta_final: str

    # --- Acciones con efectos secundarios (Pre-entrega 7) -------------------

    #: `None` si la solicitud era solo de consulta.
    accion_propuesta: AccionPropuesta | None
    #: Lo escribe el nodo `aprobacion_humana` al reanudarse.
    aprobacion: Aprobacion | None
    resultado_accion: ResultadoAccion | None


def ultima_contribucion(
    contribuciones: list[Contribucion], agente: Especialista
) -> Contribucion | None:
    """La intervención más reciente de `agente`, o `None` si nunca intervino.

    Es la regla con la que se resuelven los conflictos **dentro** de un mismo
    agente: una ronda de corrección reemplaza a la anterior, no se promedia con
    ella.
    """
    for contribucion in reversed(contribuciones):
        if contribucion["agente"] == agente:
            return contribucion
    return None


def rondas_de(contribuciones: list[Contribucion], agente: Especialista) -> int:
    """Cuántas veces intervino `agente`."""
    return sum(1 for c in contribuciones if c["agente"] == agente)
