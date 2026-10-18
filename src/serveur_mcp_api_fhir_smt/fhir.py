"""Gestion des opérations FHIR R4 indépendamment de la terminologie.

Toutes les opérations prennent l'URL canonique du CodeSystem, du ValueSet ou du
ConceptMap visé. L'expansion d'une terminologie entière ou d'une partie de sa
hiérarchie passe par un ValueSet transmis en ligne (`POST ValueSet/$expand`) :
contrairement aux ValueSet implicites (`?fhir_vs`), réservés à SNOMED CT et
LOINC, ce mécanisme fonctionne pour toutes les terminologies du serveur.
"""

from __future__ import annotations

import html
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import config
from .http_client import HttpClient, HttpRequestError

_ECL_MARKER = "fhir_vs=ecl/"

SNOMED_SYSTEM = "http://snomed.info/sct"
# Associations historiques que la spécification FHIR expose en ConceptMaps
# implicites (`?fhir_cm=<refset>`) ; les autres (WAS A, POSSIBLY REPLACED BY…) ne
# sont pas disponibles sur le SMT
SNOMED_ASSOCIATIONS: dict[str, str] = {
    "900000000000526001": "remplacé par (REPLACED BY)",
    "900000000000527005": "identique à (SAME AS)",
    "900000000000523009": "peut-être équivalent à (POSSIBLY EQUIVALENT TO)",
    "900000000000530003": "alternative (ALTERNATIVE)",
}
# Refset des indicateurs d'inactivation : absent de la spécification FHIR, mais
# exposé en ConceptMap implicite par certains serveurs (Ontoserver)
SNOMED_INACTIVATION_INDICATOR_REFSET = "900000000000489007"
UNSUPPORTED_INACTIVATION_REASON = "Raison d'inactivation non gérée en FHIR"


def unescape_ecl(value_set_url: str) -> str:
    """Annule l'échappement HTML du segment ECL d'une URL de ValueSet implicite.

    Certains clients échappent les arguments (`&lt;&lt;73211009` au lieu de
    `<<73211009`). Seul le segment après `fhir_vs=ecl/` est traité : ECL n'a pas
    d'opérateur `&`, il n'y a donc rien à corrompre.
    """
    marker_at = value_set_url.find(_ECL_MARKER)
    if marker_at == -1:
        return value_set_url
    split_at = marker_at + len(_ECL_MARKER)
    return value_set_url[:split_at] + html.unescape(value_set_url[split_at:])


# ── Modèles de résultat ─────────────────────────────────────────────────


class ConceptProperty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    value: Any = None


class Designation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    language: str | None = None
    use: str | None = None


class ExpandItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    display: str | None = None
    system: str | None = None
    version: str | None = None
    inactive: bool | None = None


class ExpandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_set_url: str | None = None
    system: str | None = None
    filter: str | None = None
    offset: int = 0
    count: int | None = None
    total: int | None = None
    returned: int = 0
    summary_only: bool = False
    contains: list[ExpandItem] = Field(default_factory=list)


class SnomedReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    association: str
    code: str | None = None
    display: str | None = None
    equivalence: str | None = None


class InactivatedSnomedConcept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    display: str | None = None
    # effectiveTime du concept : date de la version qui l'a inactivé (AAAAMMJJ)
    effective_time: str | None = None
    # « code |libellé| » de la raison, ou UNSUPPORTED_INACTIVATION_REASON
    inactivation_reason: str
    replacement: list[SnomedReplacement] = Field(default_factory=list)
    message: str | None = None


class LookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    found: bool = True
    system: str | None = None
    version: str | None = None
    name: str | None = None
    display: str | None = None
    inactive: bool | None = None
    designations: list[Designation] = Field(default_factory=list)
    properties: list[ConceptProperty] = Field(default_factory=list)
    message: str | None = None


class SubsumesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code_a: str
    code_b: str
    outcome: str
    system: str | None = None
    version: str | None = None


class TranslateMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equivalence: str | None = None
    system: str | None = None
    code: str | None = None
    display: str | None = None
    concept_map: str | None = None


class TranslateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    system: str
    result: bool
    message: str | None = None
    matches: list[TranslateMatch] = Field(default_factory=list)


class ValidateCodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    result: bool
    system: str | None = None
    version: str | None = None
    display: str | None = None
    value_set_url: str | None = None
    message: str | None = None


# ── Service ─────────────────────────────────────────────────────────────


class TerminologyService:
    def __init__(
        self,
        client: HttpClient,
        *,
        display_language: str | None = config.DEFAULT_DISPLAY_LANGUAGE,
    ) -> None:  # noqa
        self.client = client
        self.display_language = display_language

    def lookup(
        self,
        *,
        system: str,
        code: str,
        version: str | None = None,
        properties: list[str] | None = None,
        display_language: str | None = None,
    ) -> LookupResult:
        params = {
            "system": system,
            "code": code,
            "version": version,
            "property": properties or None,
            "displayLanguage": display_language or self.display_language,
        }
        try:
            data = self.client.get("CodeSystem/$lookup", params)
        except HttpRequestError as exc:
            # Un code inconnu est une réponse normale, pas un échec de l'outil
            if _is_code_not_found(exc, code):
                return LookupResult(
                    code=code,
                    found=False,
                    system=system,
                    version=version,
                    message=(
                        f"Le code '{code}' est introuvable dans cette terminologie. "
                        "Vérifiez le code, ou recherchez le concept par libellé avec "
                        "smt_expand."
                    ),
                )
            raise
        parsed = parse_parameters(data)
        props = [
            ConceptProperty(code=str(p.get("code")), value=p.get("value"))
            for p in parsed.get("property", [])
            if isinstance(p, dict) and p.get("code") is not None
        ]
        inactive = next((p.value for p in props if p.code == "inactive"), None)
        return LookupResult(
            code=code,
            system=_first_str(parsed.get("system")) or system,
            version=_first_str(parsed.get("version")),
            name=_first_str(parsed.get("name")),
            display=_first_str(parsed.get("display")),
            inactive=inactive if isinstance(inactive, bool) else None,
            designations=[
                Designation(
                    value=d.get("value"),
                    language=d.get("language"),
                    use=_coding_label(d.get("use")),
                )
                for d in parsed.get("designation", [])
                if isinstance(d, dict)
            ],
            properties=[p for p in props if p.code != "inactive"],
        )

    def validate_code(
        self,
        *,
        system: str,
        code: str,
        version: str | None = None,
        display: str | None = None,
        value_set_url: str | None = None,
    ) -> ValidateCodeResult:
        if value_set_url:
            path = "ValueSet/$validate-code"
            params = {
                "url": value_set_url,
                "system": system,
                "systemVersion": version,
                "code": code,
                "display": display,
            }
        else:
            path = "CodeSystem/$validate-code"
            params = {
                "url": system,
                "version": version,
                "code": code,
                "display": display,
            }
        parsed = parse_parameters(self.client.get(path, params))
        return ValidateCodeResult(
            code=code,
            result=bool(_first(parsed.get("result"))),
            system=_first_str(parsed.get("system")) or system,
            version=_first_str(parsed.get("version")),
            display=_first_str(parsed.get("display")),
            value_set_url=value_set_url,
            message=_first_str(parsed.get("message")),
        )

    def subsumes(
        self, *, system: str, code_a: str, code_b: str, version: str | None = None
    ) -> SubsumesResult:
        params = {
            "system": system,
            "codeA": code_a,
            "codeB": code_b,
            "version": version,
        }
        parsed = parse_parameters(self.client.get("CodeSystem/$subsumes", params))
        return SubsumesResult(
            code_a=code_a,
            code_b=code_b,
            outcome=_first_str(parsed.get("outcome")) or "unknown",
            system=system,
            version=version,
        )

    def expand(
        self,
        *,
        value_set_url: str | None = None,
        system: str | None = None,
        version: str | None = None,
        concept_filters: list[dict[str, str]] | None = None,
        filter: str | None = None,
        offset: int = 0,
        count: int = 20,
        summary_only: bool = False,
        display_language: str | None = None,
    ) -> ExpandResult:
        """Étend un ValueSet publié (`value_set_url`) ou une terminologie (`system`).

        `concept_filters` restreint l'expansion d'une terminologie à une partie de
        sa hiérarchie (filtres `compose.include.filter` de FHIR).
        """
        if offset < 0:
            raise ValueError("offset doit être positif ou nul")
        if count < 1:
            raise ValueError("count doit être supérieur ou égal à 1")
        if not value_set_url and not system:
            raise ValueError(
                "Indiquez value_set_url (ValueSet publié) ou system (terminologie)"
            )

        language = display_language or self.display_language
        params: dict[str, Any] = {
            "filter": filter or None,
            "offset": offset,
            # count=0 renvoie seulement le total : utilisé pour summary_only
            "count": 0 if summary_only else count,
            "displayLanguage": language,
        }
        resolved_url: str | None = None
        if value_set_url:
            resolved_url = unescape_ecl(value_set_url.strip())
            data = self.client.get("ValueSet/$expand", {"url": resolved_url, **params})
        else:
            include: dict[str, Any] = {"system": system}
            if version:
                include["version"] = version
            if concept_filters:
                include["filter"] = concept_filters
            body = {
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
            data = self.client.post("ValueSet/$expand", body, params)

        if data.get("resourceType") != "ValueSet":
            raise HttpRequestError(
                "ValueSet/$expand n'a pas renvoyé de ressource ValueSet"
            )
        expansion = data.get("expansion")
        if not isinstance(expansion, dict):
            expansion = {}
        contains = [] if summary_only else _parse_contains(expansion.get("contains"))
        server_offset = _as_int(expansion.get("offset"))
        return ExpandResult(
            value_set_url=resolved_url,
            system=system if not value_set_url else None,
            filter=filter,
            offset=server_offset if server_offset is not None else offset,
            count=count,
            total=_as_int(expansion.get("total")),
            returned=len(contains),
            summary_only=summary_only,
            contains=contains,
        )

    def translate(
        self,
        *,
        system: str,
        code: str,
        version: str | None = None,
        concept_map_url: str | None = None,
        target_system: str | None = None,
        reverse: bool = False,
    ) -> TranslateResult:
        params = {
            "url": concept_map_url,
            "system": system,
            "version": version,
            "code": code,
            "targetsystem": target_system,
            "reverse": True if reverse else None,
        }
        parsed = parse_parameters(self.client.get("ConceptMap/$translate", params))
        matches: list[TranslateMatch] = []
        for match in parsed.get("match", []):
            if not isinstance(match, dict):
                continue
            concept = match.get("concept")
            if not isinstance(concept, dict):
                concept = {}
            matches.append(
                TranslateMatch(
                    equivalence=match.get("equivalence"),
                    system=concept.get("system"),
                    code=concept.get("code"),
                    display=concept.get("display"),
                    concept_map=match.get("source"),
                )
            )
        return TranslateResult(
            code=code,
            system=system,
            result=bool(_first(parsed.get("result"))),
            message=_first_str(parsed.get("message")),
            matches=matches,
        )

    def snomed_inactivation(
        self,
        *,
        code: str,
        version: str | None = None,
        display_language: str | None = None,
    ) -> InactivatedSnomedConcept:
        """Raison d'inactivation et remplacements d'un concept SNOMED CT inactif.

        Le statut est vérifié par `$lookup` : un concept actif ou inconnu lève une
        `ValueError`, renvoyée au modèle comme erreur explicite. La raison et les
        remplacements sont lus avec `$translate` sur les ConceptMaps implicites
        (`?fhir_cm=<refset>`).
        """
        concept = self.lookup(
            system=SNOMED_SYSTEM,
            code=code,
            version=version,
            display_language=display_language,
        )
        if not concept.found:
            raise ValueError(
                f"Le concept SNOMED CT '{code}' est introuvable. Vérifiez le code "
                "(SCTID) ou recherchez le concept par libellé avec smt_expand."
            )
        if concept.inactive is not True:
            raise ValueError(
                f"Le concept SNOMED CT '{code}' ({concept.display}) est actif : il n'a "
                "ni raison d'inactivation ni remplacement. Utilisez smt_lookup "
                "pour consulter un concept actif."
            )

        # Les ConceptMaps implicites suivent l'édition ou la version demandée
        base = (
            version if version and version.startswith(SNOMED_SYSTEM) else SNOMED_SYSTEM
        )
        reasons = self._implicit_map(base, SNOMED_INACTIVATION_INDICATOR_REFSET, code)
        reason = reasons[0] if reasons else None
        replacements = [
            SnomedReplacement(
                association=label,
                code=match.code,
                display=match.display,
                equivalence=match.equivalence,
            )
            for refset, label in SNOMED_ASSOCIATIONS.items()
            for match in self._implicit_map(base, refset, code) or []
        ]
        effective_time = next(
            (p.value for p in concept.properties if p.code == "effectiveTime"), None
        )
        return InactivatedSnomedConcept(
            code=code,
            display=concept.display,
            effective_time=_str_or_none(effective_time),
            inactivation_reason=(
                f"{reason.code} |{reason.display or ''}|"
                if reason
                else UNSUPPORTED_INACTIVATION_REASON
            ),
            replacement=replacements,
            message=(
                None
                if replacements
                else "Aucun remplacement trouvé parmi les associations exposées en "
                "FHIR (REPLACED BY, SAME AS, POSSIBLY EQUIVALENT TO, ALTERNATIVE)."
            ),
        )

    def _implicit_map(
        self, base: str, refset: str, code: str
    ) -> list[TranslateMatch] | None:
        """Correspondances d'un code dans le ConceptMap implicite d'un refset.

        Renvoie None si le serveur ne propose pas ce ConceptMap ; les autres
        erreurs (serveur indisponible…) sont propagées.
        """
        try:
            result = self.translate(
                system=SNOMED_SYSTEM,
                code=code,
                concept_map_url=f"{base}?fhir_cm={refset}",
            )
        except HttpRequestError as exc:
            if exc.status_code in (400, 404):
                return None
            raise
        return [m for m in result.matches if m.code]


# ── Analyse des ressources FHIR ─────────────────────────────────────────


def parse_parameters(data: dict[str, Any]) -> dict[str, list[Any]]:
    """Convertit une ressource Parameters en `{nom: [valeurs]}`.

    Les paramètres composés (`part`) deviennent des dictionnaires `{nom: valeur}`.
    """
    if data.get("resourceType") != "Parameters":
        raise HttpRequestError(
            "L'opération FHIR n'a pas renvoyé de ressource Parameters"
        )
    return _parse_parts(data.get("parameter", []))


def _parse_parts(parts: Any) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    if not isinstance(parts, list):
        return out
    for param in parts:
        if not isinstance(param, dict) or not isinstance(param.get("name"), str):
            continue
        out.setdefault(param["name"], []).append(_param_value(param))
    return out


def _param_value(param: dict[str, Any]) -> Any:
    if "part" in param:
        return {name: values[0] for name, values in _parse_parts(param["part"]).items()}
    for key, value in param.items():
        if key.startswith("value"):
            return value
    return param.get("resource")


def _parse_contains(raw: Any) -> list[ExpandItem]:
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        items.append(
            ExpandItem(
                code=_str_or_none(item.get("code")),
                display=_str_or_none(item.get("display")),
                system=_str_or_none(item.get("system")),
                version=_str_or_none(item.get("version")),
                inactive=item.get("inactive")
                if isinstance(item.get("inactive"), bool)
                else None,
            )
        )
    return items


def _coding_label(value: Any) -> str | None:
    if isinstance(value, dict):
        return _str_or_none(value.get("display")) or _str_or_none(value.get("code"))
    return _str_or_none(value)


def _first(values: list[Any] | None) -> Any:
    return values[0] if values else None


def _first_str(values: list[Any] | None) -> str | None:
    return _str_or_none(_first(values))


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_code_not_found(exc: HttpRequestError, code: str) -> bool:
    """Vrai seulement si le serveur indique que *ce code* est inconnu.

    Volontairement strict : une terminologie inconnue ou une URL de base erronée
    doivent rester des erreurs, pas devenir un faux « code introuvable ».
    """
    if exc.status_code != 404:
        return False
    message = str(exc).lower()
    return f"code {code.lower()} " in message and "not found" in message
