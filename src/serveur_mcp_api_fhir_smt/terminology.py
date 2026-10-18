"""Catalogue des ressources terminologiques publiées sur le serveur.

La recherche FHIR par titre est peu fiable sur ce type de serveur (`title:contains`
fait une recherche de sous-chaîne sensible à la casse : « CIM » trouve
« speCIMen » mais pas la CIM-10). Le catalogue charge donc une fois la liste
résumée des CodeSystem, ValueSet et ConceptMap, la garde en cache, et la filtre
localement sans tenir compte de la casse ni des accents.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from . import config
from .http_client import HttpClient

logger = logging.getLogger(__name__)

ResourceType = Literal["CodeSystem", "ValueSet", "ConceptMap"]

_ELEMENTS: dict[str, str] = {
    "CodeSystem": "url,version,name,title,status,content,count",
    "ValueSet": "url,version,name,title,status",
    "ConceptMap": "url,version,name,title,status,sourceUri,sourceCanonical,"
    "targetUri,targetCanonical",
}


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    version: str | None = None
    name: str | None = None
    title: str | None = None
    status: str | None = None
    content: str | None = None
    concept_count: int | None = None
    source: str | None = None
    target: str | None = None


class TerminologyNotFoundError(LookupError):
    pass


def normalize(text: str) -> str:
    """Minuscules sans accents, pour une recherche tolérante."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", normalize(text)) if t]


def _entry_from_resource(resource: dict[str, Any]) -> CatalogEntry | None:
    url = resource.get("url")
    if not isinstance(url, str) or not url:
        return None

    def text(key: str) -> str | None:
        value = resource.get(key)
        return value if isinstance(value, str) else None

    count = resource.get("count")
    return CatalogEntry(
        url=url,
        version=text("version"),
        name=text("name"),
        title=text("title"),
        status=text("status"),
        content=text("content"),
        concept_count=count if isinstance(count, int) else None,
        source=text("sourceUri") or text("sourceCanonical"),
        target=text("targetUri") or text("targetCanonical"),
    )


class TerminologyCatalog:
    def __init__(
        self,
        client: HttpClient,
        *,
        ttl_seconds: int = config.CATALOG_CACHE_TTL_SECONDS,
        page_size: int = config.CATALOG_PAGE_SIZE,
    ) -> None:
        self.client = client
        self.ttl_seconds = ttl_seconds
        self.page_size = page_size
        self._entries: dict[str, tuple[float, list[CatalogEntry]]] = {}
        self._lock = threading.Lock()

    def _load(self, resource_type: ResourceType) -> list[CatalogEntry]:
        entries: list[CatalogEntry] = []
        next_url: str | None = resource_type
        params: dict[str, Any] | None = {
            "_elements": _ELEMENTS[resource_type],
            "_count": self.page_size,
        }
        pages = 0
        while next_url and pages < config.CATALOG_MAX_PAGES:
            bundle = self.client.get(next_url, params)
            pages += 1
            for item in bundle.get("entry", []):
                resource = item.get("resource") if isinstance(item, dict) else None
                if isinstance(resource, dict):
                    entry = _entry_from_resource(resource)
                    if entry is not None:
                        entries.append(entry)
            next_url = next(
                (
                    link.get("url")
                    for link in bundle.get("link", [])
                    if isinstance(link, dict) and link.get("relation") == "next"
                ),
                None,
            )
            params = None  # l'URL du lien « next » porte déjà les paramètres
        logger.info("Catalogue %s chargé : %d ressources", resource_type, len(entries))
        return entries

    def entries(self, resource_type: ResourceType) -> list[CatalogEntry]:
        """Liste complète (en cache) des ressources d'un type."""
        with self._lock:
            cached = self._entries.get(resource_type)
            if cached and monotonic() - cached[0] < self.ttl_seconds:
                return cached[1]
            entries = self._load(resource_type)
            self._entries[resource_type] = (monotonic(), entries)
            return entries

    def search(
        self,
        resource_type: ResourceType,
        *,
        query: str | None = None,
        url: str | None = None,
        source: str | None = None,
        target: str | None = None,
    ) -> list[CatalogEntry]:
        """Filtre le catalogue.

        `query` : tous les mots doivent apparaître dans le titre, le nom ou l'URL
        (casse et accents ignorés). `url`, `source`, `target` : égalité exacte.
        """
        words = _tokens(query) if query else []
        results = []
        for entry in self.entries(resource_type):
            if url and entry.url != url:
                continue
            if source and entry.source != source:
                continue
            if target and entry.target != target:
                continue
            if words:
                haystack = set(
                    _tokens(
                        " ".join(filter(None, (entry.title, entry.name, entry.url)))
                    )
                )
                if not all(
                    any(token.startswith(w) for token in haystack) for w in words
                ):
                    continue
            results.append(entry)
        return sorted(
            results, key=lambda e: (normalize(e.title or e.name or e.url), e.url)
        )
