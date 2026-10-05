"""Búsqueda sobre la documentación de `nimbus-sdk` (el corpus de la Pre-entrega 4).

La consigna permite "una búsqueda simulada sobre tu Vector DB de pre-entregas
anteriores". Esto es la mitad **léxica** del recuperador híbrido de la N4 (BM25)
corriendo en memoria, sin Pinecone ni embeddings. Se eligió esa mitad y no la
vectorial por dos razones:

- corre sin red y sin API keys, así que los tests y la demo del flujo de
  delegación no dependen de un índice remoto;
- las preguntas de esta demo nombran códigos literales (`E4090`, `E4290`), que
  es justo donde la N4 mostró que BM25 le gana a la búsqueda vectorial.

El chunking es por sección de Markdown (`##` / `###`), no por tokens: los
documentos son cortos y cada sección habla de una sola cosa, así que la sección
es la unidad natural para citar.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from rank_bm25 import BM25Okapi

from app import config


@dataclass(frozen=True, slots=True)
class Seccion:
    fuente: str
    titulo: str
    texto: str


#: Palabras demasiado frecuentes para discriminar. Lista corta a propósito:
#: BM25 ya castiga lo frecuente vía IDF; esto solo saca el ruido más grueso.
STOPWORDS: Final[frozenset[str]] = frozenset(
    "a al como con de del el en es la las lo los no o para por que se si su un una y "
    "que cual cuales esta este esto hay mas sus sobre entre".split()
)


def tokenizar(texto: str) -> list[str]:
    """Minúsculas, sin acentos, cortando en todo lo que no sea alfanumérico.

    `E4090`, `capturar_pago` y `X-Nimbus-Signature` sobreviven como tokens
    (el guion bajo se conserva), que es lo que hace útil a BM25 en este corpus.
    """
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    ).lower()
    return [t for t in re.findall(r"[a-z0-9_]+", sin_acentos) if t not in STOPWORDS]


def dividir_en_secciones(nombre: str, markdown: str) -> list[Seccion]:
    """Parte un documento en sus secciones `##`/`###`.

    Ignora los encabezados que caen **dentro** de un bloque de código: el
    changelog tiene comentarios `# Antes (2.x)` en ejemplos de Python que un
    split ingenuo confundiría con títulos.
    """
    secciones: list[Seccion] = []
    titulo_doc = nombre
    titulo_actual = ""
    lineas: list[str] = []
    en_codigo = False

    def cerrar() -> None:
        cuerpo = "\n".join(lineas).strip()
        if cuerpo:
            titulo = f"{titulo_doc} › {titulo_actual}" if titulo_actual else titulo_doc
            secciones.append(Seccion(fuente=nombre, titulo=titulo, texto=cuerpo))

    for linea in markdown.splitlines():
        if linea.startswith("```"):
            en_codigo = not en_codigo
        if not en_codigo and linea.startswith("# "):
            titulo_doc = linea[2:].strip()
            continue
        if not en_codigo and re.match(r"#{2,3} ", linea):
            cerrar()
            titulo_actual = linea.lstrip("#").strip()
            lineas = []
            continue
        lineas.append(linea)
    cerrar()
    return secciones


class Corpus:
    """Índice BM25 en memoria sobre las secciones de la documentación."""

    def __init__(self, secciones: list[Seccion]) -> None:
        if not secciones:
            raise ValueError("El corpus está vacío.")
        self.secciones = secciones
        self._bm25 = BM25Okapi([tokenizar(f"{s.titulo}\n{s.texto}") for s in secciones])

    @classmethod
    def desde_directorio(cls, directorio: Path) -> "Corpus":
        secciones: list[Seccion] = []
        for archivo in sorted(directorio.glob("*.md")):
            secciones.extend(
                dividir_en_secciones(archivo.name, archivo.read_text(encoding="utf-8"))
            )
        return cls(secciones)

    def buscar(self, consulta: str, k: int = 3) -> list[tuple[Seccion, float]]:
        """Las `k` secciones con mayor puntaje, descartando las de puntaje 0."""
        puntajes = self._bm25.get_scores(tokenizar(consulta))
        orden = sorted(range(len(puntajes)), key=lambda i: puntajes[i], reverse=True)
        return [(self.secciones[i], float(puntajes[i])) for i in orden[:k] if puntajes[i] > 0]

    @property
    def fuentes(self) -> set[str]:
        return {s.fuente for s in self.secciones}


@lru_cache(maxsize=1)
def corpus_por_defecto() -> Corpus:
    """El corpus del repositorio, construido una sola vez por proceso."""
    return Corpus.desde_directorio(config.DIR_DOCS)
