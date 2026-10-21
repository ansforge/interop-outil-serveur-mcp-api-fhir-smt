from __future__ import annotations

from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from serveur_mcp_api_fhir_smt import config
from serveur_mcp_api_fhir_smt.guards import QueryGuards
from serveur_mcp_api_fhir_smt.mcp_app import (
    _tool_guard,
    _truncate_response,
    create_mcp_app,
)
from serveur_mcp_api_fhir_smt.runtime import ServerRuntime

from .conftest import fixture_response, snomed_inactivation_handler

CIM10 = "https://smt.esante.gouv.fr/terminologie-cim-10"

EXPECTED_TOOLS = {
    "smt_server_health",
    "smt_list_terminologies",
    "smt_list_value_sets",
    "smt_list_concept_maps",
    "smt_lookup",
    "smt_validate_code",
    "smt_subsumes",
    "smt_translate",
    "smt_snomed_get_inactivation",
    "smt_expand",
    "smt_get_parents",
    "smt_get_children",
    "smt_get_ancestors",
    "smt_get_descendants",
}


@pytest.fixture
def app_for(make_client):
    def factory(handler, **guards: Any):
        client, transport = make_client(handler)
        runtime = ServerRuntime(client=client)
        return create_mcp_app(runtime=runtime, guards=QueryGuards(**guards)), transport

    return factory


async def call(app, name: str, **arguments: Any) -> dict[str, Any]:
    result = await app.call_tool(name, arguments)
    assert not result.is_error, result.content
    return result.structured_content


@pytest.mark.anyio
async def test_tools_are_registered_read_only_and_in_french(app_for):
    app, _ = app_for(lambda r: httpx.Response(200, json={}))

    tools = {tool.name: tool for tool in await app.list_tools()}

    assert set(tools) == EXPECTED_TOOLS
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.title and tool.description
    schema = tools["smt_lookup"].input_schema
    assert set(schema["required"]) == {"system", "code"}
    assert "smt_list_terminologies" in schema["properties"]["system"]["description"]


@pytest.mark.anyio
async def test_lookup_tool(app_for):
    app, transport = app_for(lambda r: fixture_response("lookup_cim10_J45"))

    result = await call(app, "smt_lookup", system=CIM10, code="J45")

    assert result["display"] == "Asthme"
    assert transport.last.url.params["code"] == "J45"
    # Par défaut, toutes les propriétés sont demandées
    assert transport.last.url.params.get_list("property") == ["*"]


@pytest.mark.anyio
async def test_lookup_tool_restricts_properties(app_for):
    app, transport = app_for(lambda r: fixture_response("lookup_cim10_J45"))

    await call(
        app,
        "smt_lookup",
        system=CIM10,
        code="J45",
        properties=["inclusionNote", "parent"],
    )

    assert transport.last.url.params.get_list("property") == ["inclusionNote", "parent"]


@pytest.mark.anyio
async def test_expand_requires_system_or_value_set(app_for):
    app, transport = app_for(lambda r: httpx.Response(500))

    with pytest.raises(ToolError, match=r"\[E_INVALID_ARGUMENT\]"):
        await app.call_tool("smt_expand", {"filter": "asthme"})
    assert transport.requests == []


@pytest.mark.anyio
async def test_expand_blocks_expensive_ecl(app_for):
    app, transport = app_for(lambda r: httpx.Response(500))

    with pytest.raises(ToolError, match=r"\[E_QUERY_BLOCKED\]"):
        await app.call_tool(
            "smt_expand",
            {"value_set_url": "http://snomed.info/sct?fhir_vs=ecl/<<404684003"},
        )
    assert transport.requests == []


@pytest.mark.anyio
async def test_expand_count_is_capped(app_for):
    app, transport = app_for(
        lambda r: fixture_response("expand_cim10_asthme"), max_page_size=5
    )

    await call(app, "smt_expand", system=CIM10, filter="asthme", count=80)

    assert transport.last.url.params["count"] == "5"


@pytest.mark.anyio
async def test_large_result_advisory(app_for):
    app, _ = app_for(
        lambda r: fixture_response("expand_cim10_asthme"),
        large_result_warning_threshold=3,
    )

    result = await call(app, "smt_expand", system=CIM10, filter="asthme")

    assert "10 concepts" in result["_large_result_warning"]


@pytest.mark.anyio
async def test_pagination_is_bounded(app_for):
    app, transport = app_for(
        lambda r: fixture_response("expand_cim10_asthme"), max_pagination_depth=100
    )

    await call(app, "smt_expand", system=CIM10, offset=99)
    with pytest.raises(ToolError, match=r"\[E_PAGINATION_DEPTH_EXCEEDED\]"):
        await app.call_tool("smt_expand", {"system": CIM10, "offset": 100})
    with pytest.raises(ToolError, match=r"\[E_PAGINATION_DEPTH_EXCEEDED\]"):
        await app.call_tool(
            "smt_get_descendants", {"system": CIM10, "code": "X", "offset": 500}
        )
    assert len(transport.requests) == 1


@pytest.mark.anyio
async def test_identical_calls_are_served_from_cache(app_for):
    app, transport = app_for(
        lambda r: fixture_response("lookup_cim10_J45"), rate_limit_calls=1
    )

    first = await call(app, "smt_lookup", system=CIM10, code="J45")
    second = await call(app, "smt_lookup", system=CIM10, code="J45")

    assert first == second
    assert len(transport.requests) == 1  # ni requête, ni budget consommé


@pytest.mark.anyio
async def test_server_health_is_never_cached(app_for):
    app, transport = app_for(
        lambda r: httpx.Response(200, json={"resourceType": "CapabilityStatement"})
    )

    await call(app, "smt_server_health")
    await call(app, "smt_server_health")

    assert len(transport.requests) == 2


@pytest.mark.anyio
async def test_descending_traversal_is_blocked(app_for):
    app, _ = app_for(
        lambda r: fixture_response("expand_cim10_children_J45"),
        max_traversal_steps_per_minute=2,
    )

    await call(app, "smt_get_children", system=CIM10, code="J45")
    await call(app, "smt_get_children", system=CIM10, code="J45.0")
    await call(app, "smt_get_children", system=CIM10, code="J45")  # déjà vu
    with pytest.raises(ToolError, match=r"\[E_RECURSIVE_TRAVERSAL\].*get_descendants"):
        await app.call_tool("smt_get_children", {"system": CIM10, "code": "J45.1"})
    # L'alternative recommandée reste disponible
    await call(app, "smt_get_descendants", system=CIM10, code="J45.1")


@pytest.mark.anyio
async def test_ascending_traversal_is_blocked(app_for):
    app, _ = app_for(
        lambda r: fixture_response("expand_cim10_ancestors_J45.0"),
        max_traversal_steps_per_minute=2,
    )

    await call(app, "smt_get_parents", system=CIM10, code="J45.0")
    await call(app, "smt_get_parents", system=CIM10, code="J45")
    with pytest.raises(ToolError, match=r"get_parents.*get_ancestors"):
        await app.call_tool("smt_get_parents", {"system": CIM10, "code": "J40-J47"})
    await call(app, "smt_get_ancestors", system=CIM10, code="J40-J47")


@pytest.mark.anyio
async def test_rate_limit(app_for):
    app, _ = app_for(lambda r: fixture_response("lookup_cim10_J45"), rate_limit_calls=1)

    await call(app, "smt_lookup", system=CIM10, code="J45")
    with pytest.raises(ToolError, match=r"\[E_RATE_LIMIT\]"):
        await app.call_tool("smt_lookup", {"system": CIM10, "code": "J46"})


@pytest.mark.anyio
async def test_overload_opens_the_circuit_breaker(app_for):
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "30"}, text="trop de requêtes"),
            fixture_response("lookup_cim10_J45"),
        ]
    )
    app, transport = app_for(lambda r: next(responses))

    with pytest.raises(ToolError, match=r"\[E_BACKEND_UNAVAILABLE\].*HTTP 429"):
        await app.call_tool("smt_lookup", {"system": CIM10, "code": "J45"})
    with pytest.raises(ToolError, match=r"\[E_BACKEND_UNAVAILABLE\].*~\d+ s"):
        await app.call_tool("smt_lookup", {"system": CIM10, "code": "J46"})

    assert len(transport.requests) == 1  # le second appel n'a pas atteint le SMT


@pytest.mark.anyio
async def test_repeated_server_errors_open_the_circuit_breaker(app_for):
    app, transport = app_for(
        lambda r: httpx.Response(500, text="erreur"), backend_failure_threshold=2
    )

    for code in ("A", "B"):
        with pytest.raises(ToolError, match=r"\[E_BACKEND_HTTP\]"):
            await app.call_tool("smt_lookup", {"system": CIM10, "code": code})
    with pytest.raises(ToolError, match=r"\[E_BACKEND_UNAVAILABLE\]"):
        await app.call_tool("smt_lookup", {"system": CIM10, "code": "C"})

    assert len(transport.requests) == 2


@pytest.mark.anyio
async def test_snomed_inactivation_tool(app_for):
    app, _ = app_for(snomed_inactivation_handler())

    result = await call(app, "smt_snomed_get_inactivation", code="134400000")

    assert (
        result["inactivation_reason"] == "1186917008 |Classification derived component|"
    )
    assert result["replacement"][0]["code"] == "443165006"


@pytest.mark.anyio
async def test_snomed_inactivation_tool_active_concept(app_for):
    app, _ = app_for(
        snomed_inactivation_handler(lookup="lookup_snomed_active_195967001")
    )

    with pytest.raises(ToolError, match=r"\[E_INVALID_ARGUMENT\].*est actif"):
        await app.call_tool("smt_snomed_get_inactivation", {"code": "195967001"})


@pytest.mark.anyio
async def test_prolonged_outage_blocks_calls(app_for):
    clock = [1000.0]
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0) if responses else httpx.Response(500, text="panne")

    client_app, transport = app_for(
        handler,
        backend_failure_threshold=1,
        backend_cooldown_seconds=300,
        backend_block_after_seconds=300,
        clock=lambda: clock[0],
    )

    with pytest.raises(ToolError, match=r"\[E_BACKEND_HTTP\]"):
        await client_app.call_tool("smt_lookup", {"system": CIM10, "code": "A"})
    clock[0] += 300  # 5 minutes sans réussite : dernier appel test
    with pytest.raises(
        ToolError, match=r"\[E_BACKEND_BLOCKED\].*redémarrer son outil IA"
    ):
        await client_app.call_tool("smt_lookup", {"system": CIM10, "code": "B"})

    responses.append(fixture_response("lookup_cim10_J45"))  # le SMT est revenu
    with pytest.raises(ToolError, match=r"\[E_BACKEND_BLOCKED\]"):
        await client_app.call_tool("smt_lookup", {"system": CIM10, "code": "J45"})
    assert len(transport.requests) == 2  # plus aucune requête après le blocage


@pytest.mark.anyio
async def test_backend_error_is_translated(app_for):
    app, _ = app_for(lambda r: fixture_response("lookup_unknown_system"))

    with pytest.raises(ToolError, match=r"\[E_BACKEND_HTTP\].*list_terminologies"):
        await app.call_tool(
            "smt_lookup", {"system": "https://example.org/inconnu", "code": "X"}
        )


def test_tool_guard_codes():
    from serveur_mcp_api_fhir_smt.http_client import HttpRequestError

    def failing(exc: Exception):
        def fn() -> dict[str, Any]:
            raise exc

        return fn

    cases = [
        (HttpRequestError("Délai dépassé pour GET metadata"), "E_BACKEND_TIMEOUT"),
        (HttpRequestError("HTTP 401", status_code=401), "E_BACKEND_AUTH"),
        (HttpRequestError("HTTP 500", status_code=500), "E_BACKEND_HTTP"),
        (HttpRequestError("HTTP 503", status_code=503), "E_BACKEND_UNAVAILABLE"),
        (HttpRequestError("Échec de la requête"), "E_BACKEND_REQUEST"),
        (ValueError("offset doit être positif"), "E_INVALID_ARGUMENT"),
    ]
    for exc, code in cases:
        with pytest.raises(ToolError, match=rf"\[{code}\]"):
            _tool_guard(failing(exc))

    with pytest.raises(RuntimeError):
        _tool_guard(failing(RuntimeError("bug")))


def test_truncate_response():
    small = {"contains": [1, 2, 3]}
    assert _truncate_response(small) is small

    big = {
        "total": 5000,
        "returned": 5000,
        "contains": [{"code": str(i), "display": "x" * 50} for i in range(5000)],
    }
    trimmed = _truncate_response(big)

    assert trimmed["_truncated"] is True
    assert len(str(trimmed)) < config.MAX_RESPONSE_CHARS * 1.2
    assert trimmed["returned"] == len(trimmed["contains"]) < 5000
    assert len(big["contains"]) == 5000  # l'original n'est pas modifié
