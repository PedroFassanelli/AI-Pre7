"""Utilidades de línea de comandos.

    python -m app.cli sembrar            # (re)crea la base de comercios y pagos
    python -m app.cli diagrama [--png]   # Mermaid del grafo → docs/grafo.mmd (y .png)
    python -m app.cli reintentos         # lo que escribió la acción aprobada
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app import config
from app.fuentes import db

DIR_DOCS = config.BASE_DIR.parent / "docs"


def main(argv: list[str] | None = None) -> int:
    config.configurar_consola()
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("sembrar", help="(Re)crea la base de negocio con los datos fijos.")
    diagrama = sub.add_parser("diagrama", help="Exporta el diagrama del grafo.")
    diagrama.add_argument("--png", action="store_true", help="Además, el PNG (usa mermaid.ink).")
    sub.add_parser("reintentos", help="Lista los reintentos programados por acciones aprobadas.")
    args = parser.parse_args(argv)

    if args.comando == "sembrar":
        print(asyncio.run(db.sembrar()), "→", config.DB_NEGOCIO)
    elif args.comando == "diagrama":
        from app.graph import construir_grafo_para_dibujar

        grafo = construir_grafo_para_dibujar().get_graph()
        DIR_DOCS.mkdir(exist_ok=True)
        destino = DIR_DOCS / "grafo.mmd"
        destino.write_text(grafo.draw_mermaid(), encoding="utf-8")
        print(destino)
        if args.png:
            png = DIR_DOCS / "grafo.png"
            png.write_bytes(grafo.draw_mermaid_png())
            print(png)
    elif args.comando == "reintentos":
        print(json.dumps(asyncio.run(db.listar_reintentos()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
