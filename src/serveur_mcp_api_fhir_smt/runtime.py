"""État partagé du serveur et implémentation des outils (sans dépendance au SDK MCP)."""

from __future__ import annotations

from typing import Any

from .capabilities import probe_server
from .fhir import ExpandResult, TerminologyService
from .http_client import HttpClient
from .terminology import ResourceType, TerminologyCatalog

# Filtres FHIR (compose.include.filter) de navigation dans la hiérarchie. Ils sont
# définis par la spécification et pris en charge pour toute terminologie
# hiérarchique du serveur (CIM-10, SNOMED CT, CCAM…).
_CHILDREN = ("parent", "=")  # concepts dont le parent est le code
_PARENTS = ("child", "=")  # concepts dont un enfant est le code
_ANCESTORS = ("concept", "generalizes")  # ancêtres, code inclus
_DESCENDANTS = ("concept", "descendent-of")  # descendants, code exclu


class ServerRuntime:
    def __init__(self, *, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()
        self.service = TerminologyService(self.client)
        self.catalog = TerminologyCatalog(self.client)

    def close(self) -> None:
        self.client.close()

    # ── Serveur ─────────────────────────────────────────────────────────

    def server_health(self) -> dict[str, Any]:
        """État du serveur, toujours interrogé à nouveau (jamais mis en cache)."""
        return probe_server(self.client).model_dump()

    # ── Catalogue ───────────────────────────────────────────────────────

    def list_resources(
        self,
        resource_type: ResourceType,
        *,
        query: str | None = None,
        url: str | None = None,
        source: str | None = None,
        target: str | None = None,
        offset: int = 0,
        count: int = 20,
    ) -> dict[str, Any]:
        matches = self.catalog.search(
            resource_type, query=query, url=url, source=source, target=target
        )
        page = matches[offset : offset + count]
        return {
            "resource_type": resource_type,
            "query": query,
            "total": len(matches),
            "offset": offset,
            "returned": len(page),
            "items": [entry.model_dump(exclude_none=True) for entry in page],
        }

    # ── Opérations sur les codes ────────────────────────────────────────

    def lookup(
        self,
        *,
        system: str,
        code: str,
        version: str | None = None,
        display_language: str | None = None,
    ) -> dict[str, Any]:
        result = self.service.lookup(
            system=system, code=code, version=version, display_language=display_language
        )
        return result.model_dump()

    def subsumes(
        self, *, system: str, code_a: str, code_b: str, version: str | None = None
    ) -> dict[str, Any]:
        result = self.service.subsumes(
            system=system, code_a=code_a, code_b=code_b, version=version
        )
        return result.model_dump()

    def translate(
        self,
        *,
        system: str,
        code: str,
        version: str | None = None,
        concept_map_url: str | None = None,
        target_system: str | None = None,
        reverse: bool = False,
    ) -> dict[str, Any]:
        result = self.service.translate(
            system=system,
            code=code,
            version=version,
            concept_map_url=concept_map_url,
            target_system=target_system,
            reverse=reverse,
        )
        return result.model_dump()

    def snomed_inactivation(
        self,
        *,
        code: str,
        version: str | None = None,
        display_language: str | None = None,
    ) -> dict[str, Any]:
        result = self.service.snomed_inactivation(
            code=code, version=version, display_language=display_language
        )
        return result.model_dump()

    def validate_code(
        self,
        *,
        system: str,
        code: str,
        version: str | None = None,
        display: str | None = None,
        value_set_url: str | None = None,
    ) -> dict[str, Any]:
        result = self.service.validate_code(
            system=system,
            code=code,
            version=version,
            display=display,
            value_set_url=value_set_url,
        )
        return result.model_dump()

    # ── Expand ─-----────────────────────────────────────────────────────

    def expand(
        self,
        *,
        value_set_url: str | None = None,
        system: str | None = None,
        version: str | None = None,
        filter: str | None = None,
        offset: int = 0,
        count: int = 20,
        summary_only: bool = False,
        display_language: str | None = None,
    ) -> dict[str, Any]:
        result = self.service.expand(
            value_set_url=value_set_url,
            system=system,
            version=version,
            filter=filter,
            offset=offset,
            count=count,
            summary_only=summary_only,
            display_language=display_language,
        )
        return result.model_dump()

    def hierarchy(
        self,
        relation: str,
        *,
        system: str,
        code: str,
        version: str | None = None,
        filter: str | None = None,
        offset: int = 0,
        count: int = 50,
        summary_only: bool = False,
        display_language: str | None = None,
    ) -> dict[str, Any]:
        """Concepts liés à `code` : children, parents, ancestors ou descendants."""
        prop, op = {
            "children": _CHILDREN,
            "parents": _PARENTS,
            "ancestors": _ANCESTORS,
            "descendants": _DESCENDANTS,
        }[relation]
        result: ExpandResult = self.service.expand(
            system=system,
            version=version,
            concept_filters=[{"property": prop, "op": op, "value": code}],
            filter=filter,
            offset=offset,
            count=count,
            summary_only=summary_only,
            display_language=display_language,
        )
        payload = result.model_dump()
        if relation == "ancestors":
            # `generalizes` inclut toujours le concept lui-même : on le retire du
            # total, et de la page s'il y figure
            payload["contains"] = [
                c for c in payload["contains"] if c.get("code") != code
            ]
            payload["returned"] = len(payload["contains"])
            if isinstance(payload.get("total"), int) and payload["total"] > 0:
                payload["total"] -= 1
        return {"relation": relation, "code": code, **payload}
