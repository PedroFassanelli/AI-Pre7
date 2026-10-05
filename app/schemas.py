"""Modelos de entrada y salida de la API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.jobs import EstadoJob


class CrearTarea(BaseModel):
    pregunta: str = Field(
        min_length=5,
        max_length=2000,
        description="La consulta de soporte, en lenguaje natural.",
        examples=[
            "El comercio Bazar Pampa viene teniendo pagos fallidos. ¿Cuál es el código de "
            "error que más se repite, qué significa y cómo se resuelve?"
        ],
    )


class TareaEncolada(BaseModel):
    job_id: str
    estado: EstadoJob
    url_estado: str = Field(description="Dónde consultar el estado (polling).")


class DecisionAprobacion(BaseModel):
    aprobado: bool = Field(description="`true` ejecuta la acción; `false` la descarta.")
    revisor: str = Field(min_length=1, max_length=120, description="Quién decide. Queda registrado.")
    comentario: str | None = Field(default=None, max_length=1000)


class ErrorJob(BaseModel):
    tipo: str
    mensaje: str


class Job(BaseModel):
    job_id: str
    estado: EstadoJob
    pregunta: str
    creado_en: str
    actualizado_en: str | None = None
    iniciado_en: str | None = None
    reanudado_en: str | None = None
    terminado_en: str | None = None
    #: Último nodo del grafo que terminó, y el recorrido completo hasta ahora.
    nodo_actual: str | None = None
    nodos: list[str] = Field(default_factory=list)
    #: Solo en AWAITING_APPROVAL: lo que la persona tiene que aprobar.
    aprobacion_pendiente: dict[str, Any] | None = None
    aprobacion: dict[str, Any] | None = None
    respuesta: str | None = None
    task_completed: bool | None = None
    resultado_accion: dict[str, Any] | None = None
    error: ErrorJob | None = None
    duracion_segundos: float | None = None
    duracion_reanudacion_segundos: float | None = None

    @classmethod
    def desde_almacen(cls, datos: dict[str, Any]) -> "Job":
        return cls.model_validate({k: v for k, v in datos.items() if k in cls.model_fields})
