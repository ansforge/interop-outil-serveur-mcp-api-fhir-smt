from __future__ import annotations

import httpx
import pytest

from serveur_mcp_api_fhir_smt.runtime import ServerRuntime

from .conftest import fixture_response, request_json

CIM10 = "https://smt.esante.gouv.fr/terminologie-cim-10"


@pytest.fixture
def runtime_for(make_client):
    def factory(handler):
        client, transport = make_client(handler)
        return ServerRuntime(client=client), transport

    return factory


@pytest.mark.parametrize(
    ("relation", "prop", "op"),
    [
        ("children", "parent", "="),
        ("parents", "child", "="),
        ("ancestors", "concept", "generalizes"),
        ("descendants", "concept", "descendent-of"),
    ],
)
def test_hierarchy_filters(runtime_for, relation, prop, op):
    runtime, transport = runtime_for(
        lambda r: fixture_response("expand_cim10_children_J45")
    )

    result = runtime.hierarchy(relation, system=CIM10, code="J45")

    include = request_json(transport.last)["parameter"][0]["resource"]["compose"][
        "include"
    ]
    assert include == [
        {"system": CIM10, "filter": [{"property": prop, "op": op, "value": "J45"}]}
    ]
    assert result["relation"] == relation
    assert result["code"] == "J45"


def test_children_of_j45(runtime_for):
    runtime, _ = runtime_for(lambda r: fixture_response("expand_cim10_children_J45"))

    result = runtime.hierarchy("children", system=CIM10, code="J45")

    assert result["total"] == 4
    assert {c["code"] for c in result["contains"]} == {
        "J45.0",
        "J45.1",
        "J45.8",
        "J45.9",
    }


def test_ancestors_exclude_the_concept_itself(runtime_for):
    runtime, _ = runtime_for(lambda r: fixture_response("expand_cim10_ancestors_J45.0"))

    result = runtime.hierarchy("ancestors", system=CIM10, code="J45.0")

    codes = [c["code"] for c in result["contains"]]
    assert "J45.0" not in codes
    assert set(codes) == {"J45", "J40-J47", "X"}
    assert result["total"] == 3
    assert result["returned"] == 3


def test_list_resources_paginates_catalog(runtime_for):
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "url": f"https://cs.example/{i}",
                    "title": f"Terminologie {i}",
                }
            }
            for i in range(5)
        ],
    }
    runtime, _ = runtime_for(lambda r: httpx.Response(200, json=bundle))

    page = runtime.list_resources("CodeSystem", query="terminologie", offset=2, count=2)

    assert page["total"] == 5
    assert page["returned"] == 2
    assert [item["url"] for item in page["items"]] == [
        "https://cs.example/2",
        "https://cs.example/3",
    ]


def test_list_resources_filters_by_status(runtime_for):
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"url": "https://vs.example/a", "status": "active"}},
            {"resource": {"url": "https://vs.example/b", "status": "retired"}},
            {"resource": {"url": "https://vs.example/c", "status": "active"}},
            {"resource": {"url": "https://vs.example/d", "status": "draft"}},
        ],
    }
    runtime, _ = runtime_for(lambda r: httpx.Response(200, json=bundle))

    page = runtime.list_resources("ValueSet", status="active", count=1)

    assert page["status"] == "active"
    assert page["total"] == 2
    assert page["returned"] == 1
    assert runtime.list_resources("ValueSet")["total"] == 4


def test_server_health(runtime_for):
    metadata = {
        "resourceType": "CapabilityStatement",
        "fhirVersion": "4.0.1",
        "software": {"name": "Ontoserver®", "version": "6.28.0"},
        "rest": [
            {
                "resource": [
                    {
                        "type": "CodeSystem",
                        "operation": [{"name": "lookup"}, {"name": "subsumes"}],
                    },
                    {"type": "Patient", "operation": [{"name": "everything"}]},
                ]
            }
        ],
    }
    runtime, transport = runtime_for(lambda r: httpx.Response(200, json=metadata))

    health = runtime.server_health()
    runtime.server_health()

    assert health["reachable"] is True
    assert health["fhir_version"] == "4.0.1"
    assert "software_name" not in health
    assert health["operations"] == {"CodeSystem": ["lookup", "subsumes"]}
    assert len(transport.requests) == 2  # le serveur est interrogé à chaque appel


def test_server_health_unreachable(runtime_for):
    runtime, _ = runtime_for(lambda r: httpx.Response(503, text="maintenance"))

    health = runtime.server_health()

    assert health["reachable"] is False
    assert "503" in health["error"]
