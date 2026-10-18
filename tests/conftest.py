from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from serveur_mcp_api_fhir_smt.http_client import HttpClient

FIXTURES = Path(__file__).parent / "fixtures" / "smt"
BASE_URL = "https://smt.example/fhir"


def load_fixture(name: str) -> tuple[int, dict[str, Any]]:
    """Fixture capturée par dev/capture_fixtures.py : (code HTTP, corps JSON)."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data["status_code"], data["body"]


def fixture_response(name: str) -> httpx.Response:
    status, body = load_fixture(name)
    return httpx.Response(status, json=body)


Handler = Callable[[httpx.Request], httpx.Response]


class RecordingTransport(httpx.MockTransport):
    """Transport simulé qui garde la trace des requêtes reçues."""

    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(recording)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def request_json(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


def snomed_inactivation_handler(
    *,
    lookup: str = "lookup_snomed_inactive_134400000",
    reason: str = "translate_snomed_reason_134400000",
    replaced_by: str = "translate_snomed_replaced_by_134400000",
) -> Handler:
    """Simule le SMT pour l'inactivation SNOMED CT, à partir des fixtures capturées.

    `$lookup` renvoie la fixture `lookup` ; `$translate` répond selon le refset du
    ConceptMap implicite : raison d'inactivation, REPLACED BY, et aucune
    correspondance pour les autres associations.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/CodeSystem/$lookup"):
            return fixture_response(lookup)
        refset = request.url.params["url"].rsplit("fhir_cm=", 1)[1]
        if refset == "900000000000489007":
            return fixture_response(reason)
        if refset == "900000000000526001":
            return fixture_response(replaced_by)
        return fixture_response("translate_snomed_no_match_134400000")

    return handler


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def make_client() -> Callable[[Handler], tuple[HttpClient, RecordingTransport]]:
    def factory(handler: Handler) -> tuple[HttpClient, RecordingTransport]:
        transport = RecordingTransport(handler)
        return HttpClient(BASE_URL, transport=transport), transport

    return factory
