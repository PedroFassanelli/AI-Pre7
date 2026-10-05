"""Estado de los trabajos en Redis.

Cada job es un hash `nimbus:job:<id>` más una lista `nimbus:job:<id>:nodos`
con el recorrido del grafo. El ciclo de vida:

    PENDING ──► RUNNING ──┬──► DONE
       ▲                  ├──► FAILED      (excepción, timeout, worker caído)
       │                  └──► AWAITING_APPROVAL
       │                              │
       └──────── POST /approve ───────┘

Dos reglas sostienen todo:

1. **Las transiciones son atómicas** (`transicionar`, con `WATCH`/`MULTI`).
   Solo se pasa de un estado a otro si el estado actual es el esperado. Dos
   aprobaciones simultáneas del mismo job: una gana, la otra recibe 409. Un
   reintento de la cola sobre un job que ya corrió: no hace nada.
2. **Ningún job queda colgado.** Si el grafo lanza una excepción, el worker lo
   pasa a FAILED (ver `app/worker.py`). Si el worker muere sin poder hacerlo,
   el job deja de recibir latidos, y la próxima lectura lo pasa a FAILED
   (`obtener`). El cliente que hace polling siempre termina viendo un estado
   final.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable

from redis.asyncio import Redis
from redis.exceptions import WatchError

from app import config


class EstadoJob(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    DONE = "DONE"
    FAILED = "FAILED"


TERMINALES: frozenset[EstadoJob] = frozenset({EstadoJob.DONE, EstadoJob.FAILED})

#: Campos que se guardan como JSON dentro del hash.
CAMPOS_JSON: frozenset[str] = frozenset(
    {"aprobacion_pendiente", "aprobacion", "resultado_accion", "error", "task_completed"}
)

#: Campos numéricos (epoch, segundos).
CAMPOS_NUMERICOS: frozenset[str] = frozenset(
    {"latido", "duracion_segundos", "duracion_reanudacion_segundos"}
)


class JobNoEncontrado(LookupError):
    pass


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AlmacenJobs:
    def __init__(
        self,
        redis: Redis,
        *,
        prefijo: str | None = None,
        ttl_segundos: int | None = None,
        sin_latido_segundos: int | None = None,
    ) -> None:
        # `decode_responses=True` es obligatorio: el resto del módulo trabaja con str.
        self.redis = redis
        self.prefijo = prefijo or config.PREFIJO_REDIS
        self.ttl = ttl_segundos or config.TTL_JOB_SEGUNDOS
        self.sin_latido = sin_latido_segundos or config.SIN_LATIDO_SEGUNDOS

    # --- Claves -----------------------------------------------------------

    def clave(self, job_id: str) -> str:
        return f"{self.prefijo}:job:{job_id}"

    def clave_nodos(self, job_id: str) -> str:
        return f"{self.clave(job_id)}:nodos"

    @property
    def clave_indice(self) -> str:
        return f"{self.prefijo}:jobs"

    # --- Serialización ----------------------------------------------------

    @staticmethod
    def _a_redis(campos: dict[str, Any]) -> dict[str, str]:
        salida: dict[str, str] = {}
        for nombre, valor in campos.items():
            if nombre in CAMPOS_JSON:
                salida[nombre] = json.dumps(valor, ensure_ascii=False, default=str)
            elif isinstance(valor, EstadoJob):
                salida[nombre] = valor.value
            else:
                salida[nombre] = "" if valor is None else str(valor)
        return salida

    @staticmethod
    def _desde_redis(crudo: dict[str, str]) -> dict[str, Any]:
        job: dict[str, Any] = {}
        for nombre, valor in crudo.items():
            if nombre in CAMPOS_JSON:
                job[nombre] = json.loads(valor) if valor else None
            elif nombre in CAMPOS_NUMERICOS:
                job[nombre] = float(valor) if valor else None
            else:
                job[nombre] = valor or None
        return job

    # --- Operaciones ------------------------------------------------------

    async def crear(self, pregunta: str) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        momento = ahora_iso()
        campos = {
            "job_id": job_id,
            "estado": EstadoJob.PENDING,
            "pregunta": pregunta,
            "creado_en": momento,
            "actualizado_en": momento,
            "latido": time.time(),
        }
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(self.clave(job_id), mapping=self._a_redis(campos))
            pipe.expire(self.clave(job_id), self.ttl)
            pipe.zadd(self.clave_indice, {job_id: time.time()})
            await pipe.execute()
        return await self.obtener(job_id)  # type: ignore[return-value]

    async def obtener(self, job_id: str) -> dict[str, Any] | None:
        crudo = await self.redis.hgetall(self.clave(job_id))
        if not crudo:
            return None
        job = self._desde_redis(crudo)

        if job["estado"] == EstadoJob.RUNNING and self._sin_latido(job):
            # El worker murió (o se colgó) sin poder marcar el fallo. Se marca
            # acá, al leer, para que el cliente no haga polling para siempre.
            await self.fallar(
                job_id,
                "WorkerSinLatido",
                f"El job no recibió señales del worker en {self.sin_latido} s.",
                desde=(EstadoJob.RUNNING,),
            )
            return await self.obtener(job_id)

        job["nodos"] = await self.redis.lrange(self.clave_nodos(job_id), 0, -1)
        return job

    def _sin_latido(self, job: dict[str, Any]) -> bool:
        latido = job.get("latido") or 0.0
        return time.time() - latido > self.sin_latido

    async def transicionar(
        self,
        job_id: str,
        desde: Iterable[EstadoJob],
        hacia: EstadoJob,
        **campos: Any,
    ) -> bool:
        """Cambia el estado solo si el actual está en `desde`. Atómico.

        Devuelve `False` si el estado actual no era el esperado (otro proceso
        ya lo movió). Lanza `JobNoEncontrado` si el job no existe.
        """
        permitidos = {EstadoJob(e).value for e in desde}
        clave = self.clave(job_id)
        momento = ahora_iso()
        cambios = {**campos, "estado": hacia, "actualizado_en": momento, "latido": time.time()}
        if hacia in TERMINALES:
            cambios.setdefault("terminado_en", momento)

        async with self.redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(clave)
                    actual = await pipe.hget(clave, "estado")
                    if actual is None:
                        await pipe.unwatch()
                        raise JobNoEncontrado(job_id)
                    if actual not in permitidos:
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.hset(clave, mapping=self._a_redis(cambios))
                    pipe.expire(clave, self.ttl)
                    pipe.expire(self.clave_nodos(job_id), self.ttl)
                    await pipe.execute()
                    return True
                except WatchError:
                    # Alguien escribió el job entre el WATCH y el EXEC: se
                    # vuelve a leer y se decide de nuevo.
                    continue

    async def latido(self, job_id: str, nodo: str | None = None) -> None:
        """El worker avisa que sigue vivo (y, si corresponde, qué nodo terminó)."""
        clave = self.clave(job_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            campos: dict[str, Any] = {"latido": time.time(), "actualizado_en": ahora_iso()}
            if nodo:
                campos["nodo_actual"] = nodo
                pipe.rpush(self.clave_nodos(job_id), nodo)
                pipe.expire(self.clave_nodos(job_id), self.ttl)
            pipe.hset(clave, mapping=self._a_redis(campos))
            await pipe.execute()

    async def fallar(
        self,
        job_id: str,
        tipo: str,
        mensaje: str,
        *,
        desde: Iterable[EstadoJob] = (EstadoJob.PENDING, EstadoJob.RUNNING),
    ) -> bool:
        return await self.transicionar(
            job_id, desde, EstadoJob.FAILED, error={"tipo": tipo, "mensaje": mensaje[:2000]}
        )

    async def listar(self, limite: int = 20, estado: EstadoJob | None = None) -> list[dict[str, Any]]:
        """Los jobs más recientes. Limpia del índice los que ya expiraron."""
        ids = await self.redis.zrevrange(self.clave_indice, 0, max(limite * 5, 100) - 1)
        jobs: list[dict[str, Any]] = []
        for job_id in ids:
            job = await self.obtener(job_id)
            if job is None:
                await self.redis.zrem(self.clave_indice, job_id)
                continue
            if estado is None or job["estado"] == estado:
                jobs.append(job)
            if len(jobs) >= limite:
                break
        return jobs
