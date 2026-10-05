"""Herramientas de los dos especialistas y las fuentes que consultan."""

from __future__ import annotations

import pytest

from app.fuentes.corpus import corpus_por_defecto, dividir_en_secciones, tokenizar
from app.tools.analysis_tools import calcular, evaluar_expresion, perfilar_pagos, porcentaje
from app.tools.research_tools import buscar_comercio, buscar_documentacion, consultar_pagos

# --- Corpus -----------------------------------------------------------------


def test_tokenizar_conserva_codigos_y_quita_acentos():
    assert tokenizar("El código E4090 en capturar_pago()") == ["codigo", "e4090", "capturar_pago"]


def test_dividir_ignora_encabezados_dentro_de_bloques_de_codigo():
    markdown = "# Doc\n\n## Real\ntexto\n```python\n# Antes (2.x)\nx = 1\n```\n## Otra\nmás"
    secciones = dividir_en_secciones("doc.md", markdown)
    assert [s.titulo for s in secciones] == ["Doc › Real", "Doc › Otra"]
    assert "# Antes (2.x)" in secciones[0].texto


def test_corpus_carga_los_cinco_documentos_de_la_n4():
    assert len(corpus_por_defecto().fuentes) == 5


async def test_buscar_documentacion_encuentra_el_codigo_en_el_catalogo():
    resultado = await buscar_documentacion.ainvoke({"consulta": "E4090"})
    fuentes = {r["fuente"] for r in resultado["resultados"]}
    assert "03-manejo-de-errores.md" in fuentes


async def test_buscar_documentacion_sin_coincidencias_devuelve_sugerencia():
    resultado = await buscar_documentacion.ainvoke({"consulta": "zzzz qqqq"})
    assert "error" in resultado and "sugerencia" in resultado


# --- Base de datos ----------------------------------------------------------


async def test_buscar_comercio_ignora_acentos():
    resultado = await buscar_comercio.ainvoke({"nombre": "cafe tortoni"})
    assert [c["comercio_id"] for c in resultado["comercios"]] == [102, 103]


async def test_consultar_pagos_devuelve_todos_sin_recortar():
    resultado = await consultar_pagos.ainvoke({"comercio_id": 104})
    assert resultado["cantidad"] == 7
    assert sum(p["codigo_error"] == "E4090" for p in resultado["pagos"]) == 3


async def test_consultar_pagos_de_comercio_inexistente_sugiere_buscarlo():
    resultado = await consultar_pagos.ainvoke({"comercio_id": 999})
    assert "buscar_comercio" in resultado["sugerencia"]


# --- Análisis ---------------------------------------------------------------


async def _pagos_bazar_pampa() -> list[dict]:
    return (await consultar_pagos.ainvoke({"comercio_id": 104}))["pagos"]


async def test_perfilar_pagos_agrega_por_estado_y_por_codigo():
    perfil = perfilar_pagos.invoke({"pagos": await _pagos_bazar_pampa()})
    ars = perfil["por_moneda"]["ARS"]
    assert ars["total"] == {"cantidad": 7, "monto_centavos": 6_680_000}
    assert ars["por_codigo_error"]["E4090"] == {"cantidad": 3, "monto_centavos": 3_930_000}
    # El código más frecuente va primero.
    assert next(iter(ars["por_codigo_error"])) == "E4090"


async def test_perfilar_pagos_informa_registros_invalidos_y_sigue():
    pagos = await _pagos_bazar_pampa()
    pagos[0] = {**pagos[0], "estado": "fallido", "codigo_error": None}  # fallido sin código
    pagos[1] = {**pagos[1], "monto_centavos": 129.9}  # float en vez de centavos
    perfil = perfilar_pagos.invoke({"pagos": pagos})
    assert perfil["registros_validos"] == 5
    assert {i["pago_id"] for i in perfil["invalidos"]} == {"pay_10401", "pay_10402"}


def test_perfilar_pagos_no_suma_monedas_distintas():
    pagos = [
        {"pago_id": "pay_1", "monto_centavos": 100, "moneda": "ARS", "estado": "aprobado"},
        {"pago_id": "pay_2", "monto_centavos": 900, "moneda": "CLP", "estado": "aprobado"},
    ]
    perfil = perfilar_pagos.invoke({"pagos": pagos})
    assert set(perfil["por_moneda"]) == {"ARS", "CLP"}
    assert perfil["por_moneda"]["ARS"]["total"]["monto_centavos"] == 100


def test_perfilar_pagos_sin_ningun_valido_devuelve_error():
    resultado = perfilar_pagos.invoke({"pagos": [{"pago_id": "x"}]})
    assert "error" in resultado and "sugerencia" in resultado


@pytest.mark.parametrize(
    ("expresion", "esperado"),
    [("(890000 + 2150000) / 100", 30400.0), ("2 ** 10", 1024), ("-3 + 5", 2), ("58,83 * 2", 117.66)],
)
def test_calcular_aritmetica(expresion, esperado):
    assert calcular.invoke({"expresion": expresion})["resultado"] == pytest.approx(esperado)


@pytest.mark.parametrize(
    "expresion", ["__import__('os').system('echo hola')", "abs(-1)", "x + 1", "10 ** 1000"]
)
def test_calcular_rechaza_todo_lo_que_no_es_aritmetica(expresion):
    assert "error" in calcular.invoke({"expresion": expresion})


def test_evaluar_expresion_no_usa_eval():
    with pytest.raises(ValueError):
        evaluar_expresion("[].__class__")


def test_calcular_division_por_cero():
    assert calcular.invoke({"expresion": "1 / 0"})["error"] == "División por cero."


def test_porcentaje():
    assert porcentaje.invoke({"parte": 3, "total": 7})["porcentaje"] == 42.86
    assert porcentaje.invoke({"parte": 3_930_000, "total": 6_680_000})["porcentaje"] == 58.83


def test_porcentaje_detecta_parte_mayor_que_total():
    assert "invertidos" in porcentaje.invoke({"parte": 8, "total": 7})["sugerencia"]
