from __future__ import annotations

import httpx

from serveur_mcp_api_fhir_smt.terminology import TerminologyCatalog, normalize

from .conftest import BASE_URL, fixture_response

PAGE_2 = {
    "resourceType": "Bundle",
    "entry": [
        {
            "resource": {
                "resourceType": "CodeSystem",
                "url": "https://smt.esante.gouv.fr/terminologie-cim-10",
                "version": "2025-01-01",
                "name": "CIM_10_FR_PMSI",
                "title": "Classification internationale des maladies - 10ème révision",
                "count": 19075,
            }
        },
        {
            "resource": {
                "resourceType": "CodeSystem",
                "url": "https://mos.esante.gouv.fr/NOS/TRE_R38-SpecialiteOrdinale/FHIR/x",
                "name": "TRE_R38_SpecialiteOrdinale",
                "title": "Spécialité ordinale",
            }
        },
        {"resource": {"resourceType": "CodeSystem"}},  # sans URL : ignorée
    ],
}


def paged_handler(request: httpx.Request) -> httpx.Response:
    if "_getpages" in request.url.params:
        return httpx.Response(200, json=PAGE_2)
    status, body = 200, fixture_response("codesystem_bundle_page").json()
    # Le lien « next » capturé pointe vers le SMT : redirigé vers le serveur de test
    for link in body["link"]:
        if link["relation"] == "next":
            link["url"] = f"{BASE_URL}?_getpages=abc&_getpagesoffset=3&_count=3"
    return httpx.Response(status, json=body)


def make_catalog(make_client, handler=paged_handler, ttl: int = 3600):
    client, transport = make_client(handler)
    return TerminologyCatalog(client, ttl_seconds=ttl, page_size=3), transport


def test_load_follows_next_links(make_client):
    catalog, transport = make_catalog(make_client)

    entries = catalog.entries("CodeSystem")

    assert len(transport.requests) == 2
    first = transport.requests[0]
    assert first.url.params["_count"] == "3"
    assert "count" in first.url.params["_elements"]
    assert len(entries) == 5
    cim = next(e for e in entries if e.name == "CIM_10_FR_PMSI")
    assert cim.concept_count == 19075


def test_entries_are_cached(make_client):
    catalog, transport = make_catalog(make_client)

    catalog.entries("CodeSystem")
    catalog.entries("CodeSystem")

    assert len(transport.requests) == 2  # une seule série de pages


def test_search_ignores_case_and_accents(make_client):
    catalog, _ = make_catalog(make_client)

    assert [e.name for e in catalog.search("CodeSystem", query="SPECIALITE")] == [
        "TRE_R38_SpecialiteOrdinale"
    ]
    assert [e.name for e in catalog.search("CodeSystem", query="cim 10")] == [
        "CIM_10_FR_PMSI"
    ]
    # « speCIMen » ne correspond pas à « cim » : recherche par début de mot
    assert catalog.search("CodeSystem", query="cim 11") == []


def test_search_exact_url_and_sorting(make_client):
    catalog, _ = make_catalog(make_client)

    url = "https://smt.esante.gouv.fr/terminologie-cim-10"
    assert [e.url for e in catalog.search("CodeSystem", url=url)] == [url]

    titles = [
        normalize(e.title or e.name or e.url) for e in catalog.search("CodeSystem")
    ]
    assert titles == sorted(titles)


def test_concept_map_source_and_target(make_client):
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "url": "https://cm.example/a",
                    "sourceCanonical": "https://cs.example/nuva",
                    "targetCanonical": "https://cs.example/atc",
                }
            },
            {
                "resource": {
                    "url": "https://cm.example/b",
                    "sourceUri": "https://cs.example/x",
                }
            },
        ],
    }
    catalog, transport = make_catalog(
        make_client, lambda r: httpx.Response(200, json=bundle)
    )

    matches = catalog.search("ConceptMap", source="https://cs.example/nuva")

    assert [m.url for m in matches] == ["https://cm.example/a"]
    assert matches[0].target == "https://cs.example/atc"
    assert "targetCanonical" in transport.last.url.params["_elements"]
