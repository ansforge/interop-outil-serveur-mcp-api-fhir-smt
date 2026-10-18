from __future__ import annotations

import httpx
import pytest

from serveur_mcp_api_fhir_smt import config
from serveur_mcp_api_fhir_smt.http_client import (
    HttpClient,
    HttpRequestError,
    outcome_message,
    parse_retry_after,
)

from .conftest import BASE_URL, fixture_response


def test_get_drops_none_params_and_sets_headers(make_client):
    client, transport = make_client(lambda r: httpx.Response(200, json={"ok": True}))

    assert client.get("CodeSystem/$lookup", {"code": "J45", "version": None}) == {
        "ok": True
    }

    request = transport.last
    assert str(request.url) == f"{BASE_URL}/CodeSystem/$lookup?code=J45"
    assert request.headers["Accept"] == "application/fhir+json"
    assert request.headers["User-Agent"].startswith("serveur-mcp-api-fhir-smt/")
    assert "Authorization" not in request.headers


def test_repeated_params(make_client):
    client, transport = make_client(lambda r: httpx.Response(200, json={}))

    client.get("CodeSystem/$lookup", {"property": ["parent", "child"]})

    assert transport.last.url.params.get_list("property") == ["parent", "child"]


def test_targets_smt_by_default():
    client = HttpClient()

    assert client.base_url == config.FHIR_BASE_URL == "https://smt.esante.gouv.fr/fhir"


def test_post_sends_fhir_json(make_client):
    client, transport = make_client(lambda r: httpx.Response(200, json={}))

    client.post("ValueSet/$expand", {"resourceType": "Parameters"}, {"count": 5})

    assert transport.last.method == "POST"
    assert transport.last.headers["Content-Type"] == "application/fhir+json"
    assert transport.last.url.params["count"] == "5"


def test_operation_outcome_message_without_request_id(make_client):
    client, _ = make_client(lambda r: fixture_response("lookup_unknown_system"))

    with pytest.raises(HttpRequestError) as excinfo:
        client.get("CodeSystem/$lookup", {"system": "https://example.org/inconnu"})

    error = excinfo.value
    assert error.status_code == 404
    assert error.outcome is not None
    assert str(error).startswith("HTTP 404 pour GET CodeSystem/$lookup : ")
    assert "could not be resolved" in str(error)
    assert "[" not in str(error).split(" : ", 1)[1]  # identifiant de requête retiré


def test_non_json_error(make_client):
    client, _ = make_client(lambda r: httpx.Response(502, text="Bad Gateway"))

    with pytest.raises(HttpRequestError, match="HTTP 502.*Bad Gateway") as excinfo:
        client.get("metadata")

    assert excinfo.value.status_code == 502


def test_timeout_is_normalised(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("trop long", request=request)

    client, _ = make_client(handler)

    with pytest.raises(HttpRequestError, match="Délai dépassé"):
        client.get("metadata")


def test_invalid_json_success(make_client):
    client, _ = make_client(lambda r: httpx.Response(200, text="<html>"))

    with pytest.raises(HttpRequestError, match="JSON invalide"):
        client.get("metadata")


def test_follows_own_pagination_url_but_refuses_foreign_url(make_client):
    client, transport = make_client(lambda r: httpx.Response(200, json={}))

    client.get(f"{BASE_URL}?_getpages=abc&_getpagesoffset=3")
    assert transport.last.url.params["_getpages"] == "abc"

    for foreign in ("https://evil.example/fhir", f"{BASE_URL}-evil/CodeSystem"):
        with pytest.raises(HttpRequestError, match="hors du serveur"):
            client.get(foreign)


def test_outcome_message_prefers_details_text():
    outcome = {
        "issue": [
            {"severity": "information", "diagnostics": "ignoré"},
            {
                "severity": "error",
                "details": {"text": "Code inconnu"},
                "diagnostics": "x",
            },
        ]
    }

    assert outcome_message(outcome) == "Code inconnu"
    assert outcome_message({"issue": []}) is None


def test_retry_after_is_exposed(make_client):
    client, _ = make_client(
        lambda r: httpx.Response(429, headers={"Retry-After": "12"}, text="trop")
    )

    with pytest.raises(HttpRequestError) as excinfo:
        client.get("metadata")

    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == 12


def test_parse_retry_after():
    assert parse_retry_after(None) is None
    assert parse_retry_after("30") == 30
    assert parse_retry_after("pas une date") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0  # date passée
