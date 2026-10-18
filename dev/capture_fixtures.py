"""Capture des réponses réelles du SMT, utilisées comme fixtures des tests unitaires.

Usage : uv run python dev/capture_fixtures.py

Les fichiers sont écrits dans tests/fixtures/smt/. Relancez ce script quand le
serveur change de version, puis vérifiez que les tests passent toujours.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from serveur_mcp_api_fhir_smt import config

CIM10 = "https://smt.esante.gouv.fr/terminologie-cim-10"
SNOMED = "http://snomed.info/sct"
SNOMED_FR = "http://snomed.info/sct/11000315107"
NUVA = "https://smt.esante.gouv.fr/terminologie-nuva"
NUVA_ATC = "https://smt.esante.gouv.fr/fhir/ConceptMap/alignement-nuva-atc"

OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "smt"


def inline_expand(
    system: str, filters: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    include: dict[str, Any] = {"system": system}
    if filters:
        include["filter"] = filters
    return {
        "resourceType": "Parameters",
        "parameter": [
            {
                "name": "valueSet",
                "resource": {
                    "resourceType": "ValueSet",
                    "compose": {"include": [include]},
                },
            }
        ],
    }


# nom du fichier : (méthode, chemin, paramètres, corps)
REQUESTS: dict[str, tuple[str, str, dict[str, Any], dict[str, Any] | None]] = {
    "lookup_cim10_J45": (
        "GET",
        "CodeSystem/$lookup",
        {"system": CIM10, "code": "J45"},
        None,
    ),
    "lookup_cim10_unknown": (
        "GET",
        "CodeSystem/$lookup",
        {"system": CIM10, "code": "ZZZ"},
        None,
    ),
    "lookup_unknown_system": (
        "GET",
        "CodeSystem/$lookup",
        {"system": "https://example.org/inconnu", "code": "X"},
        None,
    ),
    "validate_cim10_wrong_display": (
        "GET",
        "CodeSystem/$validate-code",
        {"url": CIM10, "code": "J45.0", "display": "Foo"},
        None,
    ),
    "subsumes_cim10_J45_J45.0": (
        "GET",
        "CodeSystem/$subsumes",
        {"system": CIM10, "codeA": "J45", "codeB": "J45.0"},
        None,
    ),
    "expand_cim10_asthme": (
        "POST",
        "ValueSet/$expand",
        {"filter": "asthme", "count": 5},
        inline_expand(CIM10),
    ),
    "expand_cim10_children_J45": (
        "POST",
        "ValueSet/$expand",
        {"count": 50},
        inline_expand(CIM10, [{"property": "parent", "op": "=", "value": "J45"}]),
    ),
    "expand_cim10_ancestors_J45.0": (
        "POST",
        "ValueSet/$expand",
        {"count": 50},
        inline_expand(
            CIM10, [{"property": "concept", "op": "generalizes", "value": "J45.0"}]
        ),
    ),
    "expand_snomed_ecl_asthme": (
        "GET",
        "ValueSet/$expand",
        {"url": f"{SNOMED_FR}?fhir_vs=ecl/<<195967001", "count": 3},
        None,
    ),
    "translate_nuva_atc": (
        "GET",
        "ConceptMap/$translate",
        {"url": NUVA_ATC, "system": NUVA, "code": "VAC0802"},
        None,
    ),
    # Inactivation SNOMED CT : 134400000 est inactif et remplacé par 443165006
    "lookup_snomed_inactive_134400000": (
        "GET",
        "CodeSystem/$lookup",
        {"system": SNOMED, "code": "134400000"},
        None,
    ),
    "lookup_snomed_active_195967001": (
        "GET",
        "CodeSystem/$lookup",
        {"system": SNOMED, "code": "195967001"},
        None,
    ),
    "lookup_snomed_unknown": (
        "GET",
        "CodeSystem/$lookup",
        {"system": SNOMED, "code": "1"},
        None,
    ),
    "translate_snomed_reason_134400000": (
        "GET",
        "ConceptMap/$translate",
        {
            "url": f"{SNOMED}?fhir_cm=900000000000489007",
            "system": SNOMED,
            "code": "134400000",
        },
        None,
    ),
    "translate_snomed_replaced_by_134400000": (
        "GET",
        "ConceptMap/$translate",
        {
            "url": f"{SNOMED}?fhir_cm=900000000000526001",
            "system": SNOMED,
            "code": "134400000",
        },
        None,
    ),
    "translate_snomed_no_match_134400000": (
        "GET",
        "ConceptMap/$translate",
        {
            "url": f"{SNOMED}?fhir_cm=900000000000527005",
            "system": SNOMED,
            "code": "134400000",
        },
        None,
    ),
    # ConceptMap implicite non proposé par le serveur (association WAS A)
    "translate_snomed_unavailable_map": (
        "GET",
        "ConceptMap/$translate",
        {
            "url": f"{SNOMED}?fhir_cm=900000000000528000",
            "system": SNOMED,
            "code": "134400000",
        },
        None,
    ),
    "codesystem_bundle_page": (
        "GET",
        "CodeSystem",
        {"_elements": "url,version,name,title,status,content,count", "_count": 3},
        None,
    ),
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        base_url=config.FHIR_BASE_URL + "/",
        headers={"Accept": "application/fhir+json"},
        timeout=30,
    ) as client:
        for name, (method, path, params, body) in REQUESTS.items():
            response = client.request(method, path, params=params, json=body)
            payload = {"status_code": response.status_code, "body": response.json()}
            target = OUT_DIR / f"{name}.json"
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"{response.status_code} {name}")


if __name__ == "__main__":
    main()
