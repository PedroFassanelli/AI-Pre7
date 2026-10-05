"""API REST del orquestador multi-agente.

    POST /tasks                 → crea el job, lo encola y devuelve el job_id (202)
    GET  /tasks/{job_id}        → estado del job (polling); nunca espera al grafo
    POST /tasks/{job_id}/approve→ aprueba o rechaza la acción crítica pendiente (202)
    GET  /tasks                 → últimos jobs (filtrable por estado)
    GET  /health                → la API y Redis responden

**Ningún endpoint ejecuta el grafo ni espera a un LLM.** Todo lo que hacen es
leer y escribir Redis con el cliente asíncrono (`redis.asyncio`) y encolar en
Arq, que también es asíncrono: el event loop nunca se bloquea, y un `POST`
responde en milisegundos aunque el grafo tarde un minuto.

Uso:
    uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app import config
from app.cola import Cola, ColaArq
from app.jobs import AlmacenJobs, EstadoJob, JobNoEncontrado
from app.schemas import CrearTarea, DecisionAprobacion, Job, TareaEncolada

log = logging.getLogger("nimbus.api")


@dataclass
class Recursos:
    jobs: AlmacenJobs
    cola: Cola


@asynccontextmanager
async def recursos_de_produccion() -> AsyncIterator[Recursos]:
    redis = Redis.from_url(config.REDIS_URL, decode_responses=True)
    pool = await create_pool(RedisSettings.from_dsn(config.REDIS_URL))
    try:
        yield Recursos(jobs=AlmacenJobs(redis), cola=ColaArq(pool))
    finally:
        await pool.aclose()
        await redis.aclose()


def crear_app(
    abrir_recursos: Callable[[], "AsyncIterator[Recursos]"] = recursos_de_produccion,  # type: ignore[assignment]
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with abrir_recursos() as recursos:  # type: ignore[attr-defined]
            app.state.recursos = recursos
            yield

    app = FastAPI(
        title="Nimbus Pagos — API del orquestador multi-agente",
        version="7.0.0",
        description=(
            "Consultas de soporte resueltas por un grafo multi-agente (LangGraph) que corre en "
            "segundo plano. Las acciones con efectos secundarios quedan en pausa hasta una "
            "aprobación humana."
        ),
        lifespan=lifespan,
    )

    def recursos(request: Request) -> Recursos:
        return request.app.state.recursos

    async def job_o_404(request: Request, job_id: str) -> dict:
        job = await recursos(request).jobs.obtener(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No existe el job {job_id} (o ya expiró).")
        return job

    @app.post(
        "/tasks",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=TareaEncolada,
        summary="Encolar una consulta",
    )
    async def crear_tarea(cuerpo: CrearTarea, request: Request, response: Response) -> TareaEncolada:
        r = recursos(request)
        job = await r.jobs.crear(cuerpo.pregunta)
        try:
            await r.cola.encolar_ejecucion(job["job_id"])
        except (RedisError, OSError) as error:
            # El job existe pero nadie lo va a correr: se marca FAILED para que
            # quien lo consulte no espere para siempre.
            await r.jobs.fallar(job["job_id"], "NoSePudoEncolar", str(error))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No se pudo encolar la tarea.") from error

        url = str(request.url_for("obtener_tarea", job_id=job["job_id"]))
        response.headers["Location"] = url
        return TareaEncolada(job_id=job["job_id"], estado=job["estado"], url_estado=url)

    @app.get("/tasks/{job_id}", response_model=Job, summary="Estado de un job")
    async def obtener_tarea(job_id: str, request: Request) -> Job:
        return Job.desde_almacen(await job_o_404(request, job_id))

    @app.post(
        "/tasks/{job_id}/approve",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=Job,
        summary="Aprobar o rechazar la acción crítica pendiente",
    )
    async def aprobar(job_id: str, decision: DecisionAprobacion, request: Request) -> Job:
        r = recursos(request)
        await job_o_404(request, job_id)
        datos = decision.model_dump()
        try:
            # Atómico: si dos personas aprueban a la vez, solo una pasa.
            pasado = await r.jobs.transicionar(
                job_id, (EstadoJob.AWAITING_APPROVAL,), EstadoJob.PENDING, aprobacion=datos
            )
        except JobNoEncontrado as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No existe el job {job_id}.") from error
        if not pasado:
            actual = await r.jobs.obtener(job_id)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"El job está en {actual and actual['estado']}: solo se aprueba en AWAITING_APPROVAL.",
            )
        try:
            await r.cola.encolar_reanudacion(job_id, datos)
        except (RedisError, OSError) as error:
            await r.jobs.fallar(job_id, "NoSePudoEncolar", str(error))
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No se pudo encolar la reanudación.") from error
        return Job.desde_almacen(await job_o_404(request, job_id))

    @app.get("/tasks", response_model=list[Job], summary="Últimos jobs")
    async def listar(
        request: Request,
        estado: EstadoJob | None = None,
        limite: int = Query(20, ge=1, le=100),
    ) -> list[Job]:
        return [Job.desde_almacen(j) for j in await recursos(request).jobs.listar(limite, estado)]

    @app.get("/health", summary="La API y Redis responden")
    async def salud(request: Request) -> dict[str, str]:
        try:
            await recursos(request).jobs.redis.ping()
        except (RedisError, OSError) as error:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Redis no responde: {error}") from error
        return {"api": "ok", "redis": "ok"}

    return app


app = crear_app()
