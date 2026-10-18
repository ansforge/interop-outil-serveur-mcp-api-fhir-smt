from __future__ import annotations

import httpx
import pytest

from serveur_mcp_api_fhir_smt.fhir import (
    SNOMED_ASSOCIATIONS,
    SNOMED_SYSTEM,
    UNSUPPORTED_INACTIVATION_REASON,
    TerminologyService,
    parse_parameters,
    unescape_ecl,
)
from serveur_mcp_api_fhir_smt.http_client import HttpRequestError

from .conftest import fixture_response, request_json, snomed_inactivation_handler

CIM10 = "https://smt.esante.gouv.fr/terminologie-cim-10"


@pytest.fixture
def service_for(make_client):
    def factory(handler):
        client, transport = make_client(handler)
        return TerminologyService(client), transport

    return factory


def test_lookup_parses_designations_and_properties(service_for):
    service, transport = service_for(lambda r: fixture_response("lookup_cim10_J45"))

    result = service.lookup(system=CIM10, code="J45")

    assert transport.last.url.params["system"] == CIM10
    assert result.found is True
    assert result.display == "Asthme"
    assert result.name == "CIM_10_FR_PMSI"
    assert result.version == "2025-01-01"
    assert result.inactive is False
    assert {(p.code, p.value) for p in result.properties} >= {
        ("parent", "J40-J47"),
        ("child", "J45.0"),
    }
    assert all(p.code != "inactive" for p in result.properties)
    assert result.designations[0].language == "fr-FR"
    assert result.designations[0].use == "preferredForLanguage"


def test_lookup_unknown_code_is_not_an_error(service_for):
    service, _ = service_for(lambda r: fixture_response("lookup_cim10_unknown"))

    result = service.lookup(system=CIM10, code="ZZZ")

    assert result.found is False
    assert "ZZZ" in (result.message or "")


def test_lookup_unknown_system_still_raises(service_for):
    service, _ = service_for(lambda r: fixture_response("lookup_unknown_system"))

    with pytest.raises(HttpRequestError) as excinfo:
        service.lookup(system="https://example.org/inconnu", code="X")

    assert excinfo.value.status_code == 404


def test_lookup_display_language_default(make_client):
    client, transport = make_client(lambda r: fixture_response("lookup_cim10_J45"))
    service = TerminologyService(client, display_language="fr")

    service.lookup(system=CIM10, code="J45")
    assert transport.last.url.params["displayLanguage"] == "fr"

    service.lookup(system=CIM10, code="J45", display_language="en")
    assert transport.last.url.params["displayLanguage"] == "en"


def test_validate_code_against_code_system(service_for):
    service, transport = service_for(
        lambda r: fixture_response("validate_cim10_wrong_display")
    )

    result = service.validate_code(system=CIM10, code="J45.0", display="Foo")

    assert transport.last.url.path.endswith("/CodeSystem/$validate-code")
    assert transport.last.url.params["url"] == CIM10
    assert result.result is False
    assert result.display == "Asthme à prédominance allergique"
    assert "Wrong Display Name" in (result.message or "")


def test_validate_code_against_value_set(service_for):
    service, transport = service_for(
        lambda r: fixture_response("validate_cim10_wrong_display")
    )

    result = service.validate_code(
        system=CIM10, code="J45.0", value_set_url="https://vs.example/jdv"
    )

    params = transport.last.url.params
    assert transport.last.url.path.endswith("/ValueSet/$validate-code")
    assert (params["url"], params["system"]) == ("https://vs.example/jdv", CIM10)
    assert result.value_set_url == "https://vs.example/jdv"


def test_subsumes(service_for):
    service, transport = service_for(
        lambda r: fixture_response("subsumes_cim10_J45_J45.0")
    )

    result = service.subsumes(system=CIM10, code_a="J45", code_b="J45.0")

    assert transport.last.url.params["codeA"] == "J45"
    assert result.outcome == "subsumes"


def test_expand_code_system_posts_inline_value_set(service_for):
    service, transport = service_for(lambda r: fixture_response("expand_cim10_asthme"))

    result = service.expand(system=CIM10, filter="asthme", count=5)

    request = transport.last
    assert request.method == "POST"
    assert request.url.params["filter"] == "asthme"
    assert request.url.params["count"] == "5"
    body = request_json(request)
    value_set = body["parameter"][0]["resource"]
    assert value_set["compose"]["include"] == [{"system": CIM10}]
    assert result.total == 10
    assert result.returned == 5
    assert result.contains[0].code == "J45"
    assert result.system == CIM10
    assert result.value_set_url is None


def test_expand_with_hierarchy_filter_and_version(service_for):
    service, transport = service_for(
        lambda r: fixture_response("expand_cim10_children_J45")
    )

    service.expand(
        system=CIM10,
        version="2025-01-01",
        concept_filters=[{"property": "parent", "op": "=", "value": "J45"}],
    )

    include = request_json(transport.last)["parameter"][0]["resource"]["compose"][
        "include"
    ][0]
    assert include == {
        "system": CIM10,
        "version": "2025-01-01",
        "filter": [{"property": "parent", "op": "=", "value": "J45"}],
    }


def test_expand_summary_only_requests_count_zero(service_for):
    service, transport = service_for(lambda r: fixture_response("expand_cim10_asthme"))

    result = service.expand(system=CIM10, summary_only=True, count=50)

    assert transport.last.url.params["count"] == "0"
    assert result.summary_only is True
    assert result.contains == []
    assert result.returned == 0


def test_expand_value_set_url_uses_get_and_unescapes_ecl(service_for):
    service, transport = service_for(
        lambda r: fixture_response("expand_snomed_ecl_asthme")
    )

    result = service.expand(
        value_set_url="http://snomed.info/sct/11000315107?fhir_vs=ecl/&lt;&lt;195967001",
        count=3,
    )

    assert transport.last.method == "GET"
    assert transport.last.url.params["url"].endswith("fhir_vs=ecl/<<195967001")
    assert result.total == 128
    assert result.returned == 3
    assert result.system is None


def test_expand_argument_validation(service_for):
    service, _ = service_for(lambda r: httpx.Response(500))

    with pytest.raises(ValueError, match="value_set_url"):
        service.expand()
    with pytest.raises(ValueError, match="offset"):
        service.expand(system=CIM10, offset=-1)


def test_expand_rejects_non_value_set(service_for):
    service, _ = service_for(
        lambda r: httpx.Response(200, json={"resourceType": "Bundle"})
    )

    with pytest.raises(HttpRequestError, match="ValueSet"):
        service.expand(system=CIM10)


def test_translate(service_for):
    service, transport = service_for(lambda r: fixture_response("translate_nuva_atc"))

    result = service.translate(
        system="https://smt.esante.gouv.fr/terminologie-nuva",
        code="VAC0802",
        concept_map_url="https://smt.esante.gouv.fr/fhir/ConceptMap/alignement-nuva-atc",
    )

    assert "reverse" not in transport.last.url.params
    assert result.result is True
    match = result.matches[0]
    assert (match.code, match.equivalence) == ("J07BF01", "equivalent")
    assert match.system == "https://smt.esante.gouv.fr/terminologie-atc"
    assert match.concept_map is not None and match.concept_map.endswith(
        "alignement-nuva-atc"
    )


def test_translate_reverse_flag(service_for):
    service, transport = service_for(lambda r: fixture_response("translate_nuva_atc"))

    service.translate(system="s", code="c", target_system="t", reverse=True)

    assert transport.last.url.params["reverse"] == "true"
    assert transport.last.url.params["targetsystem"] == "t"


def test_parse_parameters_requires_parameters_resource():
    with pytest.raises(HttpRequestError):
        parse_parameters({"resourceType": "OperationOutcome"})


def test_unescape_ecl_only_touches_ecl_segment():
    url = "http://snomed.info/sct?fhir_vs=ecl/&lt;&lt;123&amp;x"
    assert unescape_ecl(url) == "http://snomed.info/sct?fhir_vs=ecl/<<123&x"
    assert (
        unescape_ecl("https://vs.example?a=1&amp;b=2")
        == "https://vs.example?a=1&amp;b=2"
    )


# ── Inactivation SNOMED CT ──────────────────────────────────────────────


def test_snomed_inactivation_reason_and_replacements(service_for):
    service, transport = service_for(snomed_inactivation_handler())

    result = service.snomed_inactivation(code="134400000")

    assert result.display == "Fragility fracture due to unspecified osteoporosis"
    assert result.effective_time == "20100131"
    assert result.inactivation_reason == "1186917008 |Classification derived component|"
    assert [r.model_dump() for r in result.replacement] == [
        {
            "association": "remplacé par (REPLACED BY)",
            "code": "443165006",
            "display": "fracture ostéoporotique",
            "equivalence": "equivalent",
        }
    ]
    assert result.message is None
    # $lookup, puis la raison et les 4 associations exposées en FHIR
    lookup, *translates = transport.requests
    assert lookup.url.params["system"] == SNOMED_SYSTEM
    assert [r.url.params["url"] for r in translates] == [
        f"{SNOMED_SYSTEM}?fhir_cm={refset}"
        for refset in ["900000000000489007", *SNOMED_ASSOCIATIONS]
    ]


def test_snomed_inactivation_follows_requested_edition(service_for):
    edition = "http://snomed.info/sct/11000315107"
    service, transport = service_for(snomed_inactivation_handler())

    service.snomed_inactivation(code="134400000", version=edition)

    lookup, *translates = transport.requests
    assert lookup.url.params["version"] == edition
    assert all(
        r.url.params["url"].startswith(f"{edition}?fhir_cm=") for r in translates
    )


def test_snomed_inactivation_rejects_active_concept(service_for):
    service, transport = service_for(
        snomed_inactivation_handler(lookup="lookup_snomed_active_195967001")
    )

    with pytest.raises(ValueError, match=r"'195967001' \(asthme\) est actif"):
        service.snomed_inactivation(code="195967001")
    assert len(transport.requests) == 1  # ni raison ni associations demandées


def test_snomed_inactivation_rejects_unknown_concept(service_for):
    service, transport = service_for(
        snomed_inactivation_handler(lookup="lookup_snomed_unknown")
    )

    with pytest.raises(ValueError, match="'1' est introuvable"):
        service.snomed_inactivation(code="1")
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "reason",
    [
        "translate_snomed_unavailable_map",  # ConceptMap non proposé (404)
        "translate_snomed_no_match_134400000",  # aucune raison pour ce concept
    ],
)
def test_snomed_inactivation_reason_not_available(service_for, reason):
    service, _ = service_for(snomed_inactivation_handler(reason=reason))

    result = service.snomed_inactivation(code="134400000")

    assert result.inactivation_reason == UNSUPPORTED_INACTIVATION_REASON
    assert result.inactivation_reason == "Raison d'inactivation non gérée en FHIR"
    assert len(result.replacement) == 1


def test_snomed_inactivation_without_replacement(service_for):
    service, _ = service_for(
        snomed_inactivation_handler(replaced_by="translate_snomed_no_match_134400000")
    )

    result = service.snomed_inactivation(code="134400000")

    assert result.replacement == []
    assert result.message is not None
    assert "REPLACED BY" in result.message


def test_snomed_inactivation_propagates_server_errors(service_for):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/CodeSystem/$lookup"):
            return fixture_response("lookup_snomed_inactive_134400000")
        return httpx.Response(503, text="maintenance")

    service, _ = service_for(handler)

    # Seuls 400/404 signifient « ConceptMap non proposé » : un 503 reste une erreur
    with pytest.raises(HttpRequestError) as excinfo:
        service.snomed_inactivation(code="134400000")
    assert excinfo.value.status_code == 503
