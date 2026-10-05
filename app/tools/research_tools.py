"""Herramientas del agente de investigación.

Todas **leen**; ninguna calcula. La separación es deliberada: si el
investigador pudiera sacar porcentajes, lo haría "de paso" y en su cabeza, y el
analista quedaría validando números que nadie computó con una herramienta.

Convención heredada de la Pre-entrega 5: cuando algo falla, la herramienta no
lanza una excepción sino que devuelve `error` + `sugerencia`, con la próxima
acción concreta. El modelo lee eso y se corrige solo dentro de su ciclo ReAct.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.fuentes import db
from app.fuentes.corpus import corpus_por_defecto

#: Caracteres de cada sección que se devuelven. Las secciones del corpus son
#: cortas; el recorte existe para que una sección larga no se coma la ventana.
MAX_CARACTERES_SECCION = 1500


class ArgsBuscarDocumentacion(BaseModel):
    consulta: str = Field(
        description=(
            "Qué buscar en la documentación. Funciona mejor con términos literales: "
            "códigos de error (E4090), nombres de función (capturar_pago), "
            "encabezados HTTP (Idempotency-Key)."
        )
    )
    k: int = Field(default=3, ge=1, le=5, description="Cuántas secciones devolver (1 a 5).")


@tool(args_schema=ArgsBuscarDocumentacion)
async def buscar_documentacion(consulta: str, k: int = 3) -> dict[str, Any]:
    """Busca en la documentación técnica oficial de nimbus-sdk.

    Devuelve las secciones más relevantes con su archivo de origen (`fuente`),
    para que puedas citarlas. Usala para saber qué significa un código de
    error, cómo se resuelve, o cómo funciona una parte de la API.
    """
    resultados = corpus_por_defecto().buscar(consulta, k=k)
    if not resultados:
        return {
            "consulta": consulta,
            "error": "Ninguna sección de la documentación coincide con esa consulta.",
            "sugerencia": (
                "Probá con términos más literales: el código de error exacto, el "
                "nombre de la excepción o de la función."
            ),
        }
    return {
        "consulta": consulta,
        "resultados": [
            {
                "fuente": seccion.fuente,
                "seccion": seccion.titulo,
                "puntaje": round(puntaje, 2),
                "texto": seccion.texto[:MAX_CARACTERES_SECCION],
            }
            for seccion, puntaje in resultados
        ],
    }


class ArgsBuscarComercio(BaseModel):
    nombre: str = Field(
        description="Nombre o parte del nombre del comercio. No distingue acentos ni mayúsculas."
    )


@tool(args_schema=ArgsBuscarComercio)
async def buscar_comercio(nombre: str) -> dict[str, Any]:
    """Busca comercios de Nimbus Pagos por nombre y devuelve su `comercio_id`.

    Necesitás el `comercio_id` para consultar los pagos de un comercio.
    """
    coincidencias = await db.buscar_comercios_por_nombre(nombre)
    if not coincidencias:
        return {
            "nombre": nombre,
            "error": "No hay ningún comercio con ese nombre.",
            "sugerencia": "Probá con una sola palabra del nombre, sin el tipo de negocio.",
        }
    return {"nombre": nombre, "comercios": coincidencias}


class ArgsConsultarPagos(BaseModel):
    comercio_id: int = Field(description="ID numérico del comercio, obtenido con buscar_comercio.")


@tool(args_schema=ArgsConsultarPagos)
async def consultar_pagos(comercio_id: int) -> dict[str, Any]:
    """Devuelve TODOS los pagos de un comercio, tal como están en la base.

    Cada pago trae `pago_id`, `monto_centavos` (entero, en centavos), `moneda`,
    `estado` (aprobado, fallido, pendiente, reembolsado), `codigo_error` (solo
    en los fallidos) y la fecha. Los datos se devuelven crudos, sin totales:
    los cálculos los hace el analista.
    """
    pagos = await db.listar_pagos(comercio_id)
    if not pagos:
        return {
            "comercio_id": comercio_id,
            "error": "Ese comercio no tiene pagos o no existe.",
            "sugerencia": "Verificá el comercio_id con buscar_comercio.",
        }
    return {"comercio_id": comercio_id, "cantidad": len(pagos), "pagos": pagos}


HERRAMIENTAS_INVESTIGACION = [buscar_documentacion, buscar_comercio, consultar_pagos]

#: Las herramientas cuyo resultado es **dato** (y por eso se le pasa al
#: analista). La documentación no está acá: el analista no la necesita para
#: calcular, y mandársela sería la contaminación de contexto que la consigna
#: pide evitar.
HERRAMIENTAS_DE_DATOS: frozenset[str] = frozenset({"consultar_pagos"})
