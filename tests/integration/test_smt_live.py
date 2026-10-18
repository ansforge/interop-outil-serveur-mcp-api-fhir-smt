"""Tests sur l'API FHIR réelle du SMT (réseau requis).

Lancement : uv run pytest -m integration
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import Any

import pytest

from serveur_mcp_api_fhir_smt.runtime import ServerRuntime

pytestmark = pytest.mark.integration

CIM10 = "https://smt.esante.gouv.fr/terminologie-cim-10"


@pytest.fixture(scope="module")
def runtime():
    rt = ServerRuntime()
    yield rt
    rt.close()


def test_server_is_reachable(runtime):
    health = runtime.server_health()

    assert health["reachable"] is True
    assert "lookup" in health["operations"]["CodeSystem"]


def test_catalog_finds_cim10(runtime):
    page = runtime.list_resources("CodeSystem", query="cim 10")

    assert CIM10 in [item["url"] for item in page["items"]]


def test_lookup_and_unknown_code(runtime):
    assert runtime.lookup(system=CIM10, code="J45")["display"] == "Asthme"
    assert runtime.lookup(system=CIM10, code="ZZZ")["found"] is False


def test_text_search(runtime):
    result = runtime.expand(system=CIM10, filter="asthme", count=5)

    assert result["total"] >= 1
    assert "J45" in [c["code"] for c in result["contains"]]


def test_hierarchy(runtime):
    children = runtime.hierarchy("children", system=CIM10, code="J45")
    ancestors = runtime.hierarchy("ancestors", system=CIM10, code="J45.0")

    assert "J45.0" in [c["code"] for c in children["contains"]]
    assert "J45" in [c["code"] for c in ancestors["contains"]]
    assert "J45.0" not in [c["code"] for c in ancestors["contains"]]


def test_snomed_ecl(runtime):
    result = runtime.expand(
        value_set_url="http://snomed.info/sct/11000315107?fhir_vs=ecl/<<195967001",
        summary_only=True,
    )

    assert result["total"] > 10


def test_snomed_inactivation(runtime):
    result = runtime.snomed_inactivation(code="134400000")

    assert result["inactivation_reason"].startswith("1186917008 |")
    assert "443165006" in [r["code"] for r in result["replacement"]]
    with pytest.raises(ValueError, match="est actif"):
        runtime.snomed_inactivation(code="195967001")


def _rpc(messages: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Dialogue JSON-RPC avec le serveur lancé en stdio ; réponses indexées par id.

    L'entrée standard reste ouverte jusqu'à la dernière réponse attendue : le
    serveur s'arrête dès qu'elle est fermée, même si un outil est en cours.
    """
    expected = {m["id"] for m in messages if "id" in m}
    proc = subprocess.Popen(
        [sys.executable, "-m", "serveur_mcp_api_fhir_smt"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    responses: dict[int, dict[str, Any]] = {}
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        while not expected <= responses.keys():
            line = proc.stdout.readline()
            if not line:
                break
            reply = json.loads(line)
            if "id" in reply:
                responses[reply["id"]] = reply
    finally:
        watchdog.cancel()
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=10)
    return responses


def test_stdio_round_trip():
    responses = _rpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "tests", "version": "0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "smt_lookup",
                    "arguments": {"system": CIM10, "code": "J45"},
                },
            },
        ]
    )

    assert responses[1]["result"]["serverInfo"]["name"] == "serveur-mcp-api-fhir-smt"
    assert len(responses[2]["result"]["tools"]) == 14
    call = responses[3]["result"]
    assert call.get("isError") is not True
    assert call["structuredContent"]["display"] == "Asthme"
