"""Capturas del dashboard de Phoenix para `screenshots/`.

    python scripts/capturas.py                  # todas
    python scripts/capturas.py --solo hitl      # una sola

Usa Playwright con el Edge (o Chrome) que ya está instalado: no hace falta
`playwright install`. Las capturas son del Phoenix real que levanta el
docker compose, con las trazas de la prueba de carga.

Cada captura navega a una vista, espera a que carguen los datos y, si hace
falta, interactúa (abrir una traza, pasar el mouse sobre el gráfico de
latencia para que aparezca el tooltip con el p95).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path

import httpx
from playwright.async_api import Page, async_playwright

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "screenshots"
PHOENIX = "http://localhost:6006"
PROYECTO = "nimbus-api"
ANCHO, ALTO = 1920, 1080


def id_proyecto() -> str:
    """El id GraphQL del proyecto (`UHJvamVjdDo...` = base64 de `Project:<n>`)."""
    r = httpx.post(f"{PHOENIX}/graphql", json={"query": "{ projects { edges { node { id name } } } }"}).json()
    for borde in r["data"]["projects"]["edges"]:
        if borde["node"]["name"] == PROYECTO:
            return borde["node"]["id"]
    raise SystemExit(f"No existe el proyecto {PROYECTO!r} en Phoenix.")


async def esperar_datos(pagina: Page, segundos: float = 3.0) -> None:
    await pagina.wait_for_load_state("networkidle")
    await pagina.wait_for_timeout(int(segundos * 1000))


async def ocultar_columnas(pagina: Page, ids: list[str]) -> None:
    """Destilda columnas de la tabla para que entren latencia, tokens y costo."""
    await pagina.get_by_role("button", name="Columns").click()
    await pagina.wait_for_timeout(500)
    for columna in ids:
        casilla = pagina.locator(f'li[data-column-id="{columna}"] input[type=checkbox]')
        if await casilla.count() and await casilla.first.is_checked():
            await casilla.first.click(force=True)
    await pagina.keyboard.press("Escape")
    await pagina.wait_for_timeout(500)


async def captura(pagina: Page, nombre: str, **kwargs) -> None:
    ruta = DESTINO / f"{nombre}.png"
    await pagina.screenshot(path=str(ruta), **kwargs)
    print("  ✔", ruta.relative_to(RAIZ))


# --- Vistas -------------------------------------------------------------------


async def trazas(pagina: Page, proyecto: str, rango: str) -> None:
    """Las 5 trazas de la prueba de carga con latencia, tokens y costo por ejecución."""
    await pagina.goto(f"{PHOENIX}/projects/{proyecto}/traces?timeRangeKey={rango}")
    await esperar_datos(pagina)
    await ocultar_columnas(pagina, ["error", "userId", "annotations", "traceAnnotations", "startTime"])
    await captura(pagina, "01-trazas-costo-por-ejecucion")


async def detalle(pagina: Page, proyecto: str, rango: str) -> None:
    """Una traza abierta: el árbol de spans (nodos del grafo, subagentes, LLM, tools)."""
    await pagina.goto(f"{PHOENIX}/projects/{proyecto}/traces?timeRangeKey={rango}")
    await esperar_datos(pagina)
    # La del comercio Bazar Pampa (pregunta 1): la que recorre los cinco nodos.
    await pagina.get_by_text("El comercio Bazar Pampa", exact=False).first.click()
    await esperar_datos(pagina, 4)
    await captura(pagina, "02-traza-arbol-de-spans")


async def tarjeta(pagina: Page, titulo: str):
    """La tarjeta del dashboard que tiene ese título (el gráfico y su leyenda)."""
    return pagina.get_by_text(titulo, exact=True).locator(
        "xpath=ancestor::*[.//*[contains(@class,'recharts-wrapper')]][1]"
    )


async def pasar_mouse_por_el_dato(pagina: Page, grafico) -> None:
    """Hover sobre el primer dato del gráfico para que Recharts muestre el tooltip."""
    dato = grafico.locator(".recharts-line-dot, .recharts-bar-rectangle path, .recharts-rectangle").first
    caja = await dato.bounding_box()
    if caja is None:
        return
    await pagina.mouse.move(caja["x"] + caja["width"] / 2, caja["y"] + caja["height"] / 2)
    await pagina.wait_for_timeout(800)


async def metricas(pagina: Page, proyecto: str, rango: str) -> None:
    """Pestaña Metrics: latencia por percentil (p95) y costo, con el tooltip abierto."""
    await pagina.goto(f"{PHOENIX}/projects/{proyecto}/metrics?timeRangeKey={rango}")
    await esperar_datos(pagina, 5)

    latencia = await tarjeta(pagina, "Trace latency")
    await pasar_mouse_por_el_dato(pagina, latencia)
    await captura(pagina, "03-latencia-p95")

    # Costo por modelo: el supervisor (gpt-4o) contra el resto (gpt-4o-mini).
    await pagina.mouse.move(5, 5)
    await pagina.wait_for_timeout(500)
    modelos = await tarjeta(pagina, "Top models by cost")
    await pasar_mouse_por_el_dato(pagina, modelos)
    await captura(pagina, "04-costo-por-modelo")


async def sesiones(pagina: Page, proyecto: str, rango: str) -> None:
    """Una sesión por job (session.id = job_id): el job con HITL tiene dos trazas."""
    await pagina.goto(f"{PHOENIX}/projects/{proyecto}/sessions?timeRangeKey={rango}")
    await esperar_datos(pagina)
    await captura(pagina, "05-sesiones-por-job")


async def hitl(pagina: Page, proyecto: str, rango: str) -> None:
    """El job con aprobación humana: su sesión (dos trazas, antes y después de la
    pausa) y la traza de la reanudación, con el span TOOL que escribe en la base."""
    await pagina.goto(f"{PHOENIX}/projects/{proyecto}/sessions?timeRangeKey={rango}")
    await esperar_datos(pagina)
    await pagina.get_by_text("Viajes Aconcagua", exact=False).first.click()
    await esperar_datos(pagina, 4)
    await captura(pagina, "06-hitl-sesion-pausa-y-reanudacion")

    await pagina.goto(f"{PHOENIX}/projects/{proyecto}/traces?timeRangeKey={rango}")
    await esperar_datos(pagina)
    await pagina.get_by_text('{"reanudacion"', exact=False).first.click()
    await esperar_datos(pagina, 3)
    # El span de la escritura: así se ve qué se ejecutó y quién lo aprobó.
    await pagina.get_by_text("programar_reintentos", exact=True).last.click()
    await esperar_datos(pagina, 2)
    await captura(pagina, "07-hitl-traza-reanudacion")


async def error(pagina: Page, proyecto: str, rango: str) -> None:
    """Un job que falló (API key inválida): la traza queda en ERROR con la excepción."""
    await pagina.goto(f"{PHOENIX}/projects/{proyecto}/traces?timeRangeKey={rango}")
    await esperar_datos(pagina)
    await pagina.get_by_text("¿Qué significa el error E4012", exact=False).first.click()
    await esperar_datos(pagina, 3)
    eventos = pagina.get_by_role("tab", name="Events")
    if await eventos.count():
        await eventos.first.click()
        await pagina.wait_for_timeout(1000)
    await captura(pagina, "08-job-failed-traza-con-error")


VISTAS = {
    "trazas": trazas,
    "detalle": detalle,
    "metricas": metricas,
    "sesiones": sesiones,
    "hitl": hitl,
    "error": error,
}


async def main(solo: list[str], rango: str, navegador: str) -> None:
    DESTINO.mkdir(exist_ok=True)
    proyecto = id_proyecto()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel=navegador, headless=True)
        contexto = await browser.new_context(viewport={"width": ANCHO, "height": ALTO}, color_scheme="dark")
        pagina = await contexto.new_page()
        for nombre in solo or VISTAS:
            print(nombre)
            await VISTAS[nombre](pagina, proyecto, rango)
        await browser.close()


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--solo", nargs="*", choices=list(VISTAS), default=[])
    parser.add_argument("--rango", default="1h", help="timeRangeKey de Phoenix: 15m, 1h, 12h, 1d, 7d...")
    parser.add_argument("--navegador", default="msedge", help="msedge o chrome")
    args = parser.parse_args()
    asyncio.run(main(args.solo, args.rango, args.navegador))
