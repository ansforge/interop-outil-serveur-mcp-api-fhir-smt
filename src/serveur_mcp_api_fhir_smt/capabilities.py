"""Sonde du serveur FHIR à partir de son CapabilityStatement (`GET /metadata`)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .http_client import HttpClient, HttpRequestError

TERMINOLOGY_RESOURCES = ("CodeSystem", "ValueSet", "ConceptMap")


class ServerStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reachable: bool
    fhir_base_url: str
    fhir_version: str | None = None
    # Opérations déclarées par type de ressource terminologique
    operations: dict[str, list[str]] = Field(default_factory=dict)
    error: str | None = None


def summarize_metadata(metadata: dict[str, Any], fhir_base_url: str) -> ServerStatus:
    operations: dict[str, list[str]] = {}
    for rest in metadata.get("rest", []) or []:
        if not isinstance(rest, dict):
            continue
        for resource in rest.get("resource", []) or []:
            if (
                isinstance(resource, dict)
                and resource.get("type") in TERMINOLOGY_RESOURCES
            ):
                operations[resource["type"]] = sorted(
                    op["name"]
                    for op in resource.get("operation", []) or []
                    if isinstance(op, dict) and isinstance(op.get("name"), str)
                )
    return ServerStatus(
        reachable=True,
        fhir_base_url=fhir_base_url,
        fhir_version=metadata.get("fhirVersion"),
        operations=operations,
    )


def probe_server(client: HttpClient) -> ServerStatus:
    """Interroge `/metadata` et en renvoie le résumé, même si le serveur est
    injoignable.
    """
    base_url = client.base_url
    try:
        metadata = client.get("metadata")
    except HttpRequestError as exc:
        return ServerStatus(reachable=False, fhir_base_url=base_url, error=str(exc))
    return summarize_metadata(metadata, base_url)
