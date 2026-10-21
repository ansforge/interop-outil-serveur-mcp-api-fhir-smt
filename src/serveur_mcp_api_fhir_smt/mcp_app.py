"""Application MCP : déclaration des outils, garde-fous et traduction des erreurs.

Les outils sont synchrones : le SDK les exécute dans un thread, ce qui permet au
client HTTP et aux garde-fous (verrous, sémaphore) d'être bloquants.
"""

from __future__ import annotations

import atexit
import json
import logging
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.server import CacheableMethod, CacheHint
from mcp.types import ToolAnnotations
from pydantic import Field

from . import config
from .guards import GuardError, QueryGuards
from .http_client import HttpRequestError
from .runtime import ServerRuntime
from .terminology import PublicationStatus, ResourceType

logger = logging.getLogger(__name__)

# La liste d'outils est fixe et identique pour tous : elle peut être mise en cache
_STATIC_LIST_CACHE_HINT = CacheHint(ttl_ms=3_600_000, scope="public")
_CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "tools/list": _STATIC_LIST_CACHE_HINT,
    "server/discover": _STATIC_LIST_CACHE_HINT,
}

_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

INSTRUCTIONS = """\
Ce serveur donne accès aux terminologies publiées sur le Serveur Multi-Terminologies \
(SMT) de l'Agence du Numérique en Santé, via son API FHIR R4. Les utilisateurs sont \
francophones : réponds en français.
Ses API FHIR peuvent être désignées comme les API FHIR du SMT, le FTS, le serveur FHIR \
de l'ANS voire le SMT.

DÉMARCHE :
1. Identifie l'URL canonique de la terminologie avec smt_list_terminologies (ou du \
jeu de valeurs avec smt_list_value_sets, de l'alignement avec smt_list_concept_maps).
Ne devine jamais une URL.
2. Utilise cette URL comme paramètre `system` (ou `value_set_url`) des autres outils.

CHOIX DE L'OUTIL :
- Retrouve un code à partir d'un libellé : smt_expand avec system + filter.
- Détail d'un code connu (libellés, propriétés, statut) : smt_lookup.
- Vérifie un code, son libellé ou son appartenance à un jeu de valeurs : \
smt_validate_code.
- Relation hiérarchique entre deux codes : smt_subsumes.
- Navigue dans la hiérarchie : smt_get_parents, smt_get_children, \
smt_get_ancestors, smt_get_descendants.
- Correspondance vers une autre terminologie : smt_translate.
- Concept SNOMED CT inactif (raison d'inactivation, remplacements) : \
smt_snomed_get_inactivation.

RÈGLES DE PERFORMANCE (le serveur est partagé) :
- Compte avant de récupérer : summary_only=true sur les expands larges.
- Ne parcoure pas les pages suivantes sans l'accord de l'utilisateur.
- N'explore pas une hiérarchie niveau par niveau en boucle : utilise \
smt_get_descendants ou smt_get_ancestors en un seul appel.
- La pagination est limitée aux premiers résultats : au-delà, affine la demande.

RÈGLES DE QUALITE :
- N'invente jamais un code, un libellé, une propriété ou n'importe quelle autre valeur.
- Les alignement ont un sens (source vers target), n'utilise jamais un alignement \
    dans l'autre sens.
- Ne déduis jamais quelque chose de la métadonnée "content" d'une Terminologie.
- Ne devine jamais la Terminologie à laquelle appartient un code, demande toujours à \
    l'utilisateur de clarifier.
- Ne modifie jamais le terme d'un code.

RÈGLES DE STYLE :
- Désigne le module français SNOMED CT comme l'édition française SNOMED CT.
- Désigne les identifiants de type http://id.who.int/icd/entity/X comme des \
    identifiants de la fondation qui sont des identifiants alternatifs aux codes MMS.
- Ne traduis jamais toi-même une notion anglaise qui pourrait être attaché à un \
    concept (e.g. le nom d'une hiérarchie).
- Ecris les libellés des concepts en italique pour les distinguer du reste du texte.
- Ajoute un avertissement à la fin de ta réponse pour les questions sur les choix ou \
    la création de jeux de valeurs indiquant qu'il est nécessaire de se référer aux \
    spécifications du CI-SIS pour un cas d'usage d'interopérabilité en France.

Les résultats sont fournis à des fins terminologiques uniquement et ne remplacent pas
 une décision clinique. L'ANS est le point de contact de référence
 (ans-terminologies@esante.gouv.fr).
"""

# ── Types de paramètres partagés ────────────────────────────────────────

System = Annotated[
    str,
    Field(
        description="URL canonique de la terminologie (CodeSystem), obtenue avec "
        "smt_list_terminologies. Ex. : https://smt.esante.gouv.fr/terminologie-cim-10, "
        "http://snomed.info/sct, http://loinc.org"
    ),
]
Code = Annotated[str, Field(description="Code du concept dans la terminologie")]
Version = Annotated[
    str | None,
    Field(
        description="Version de la terminologie (facultatif ; par défaut, la plus "
        "récente). Pour SNOMED CT, l'édition française est "
        "http://snomed.info/sct/11000315107"
    ),
]
DisplayLanguage = Annotated[
    str | None,
    Field(description="Langue des libellés, ex. 'fr' (facultatif)"),
]
Properties = Annotated[
    list[str] | None,
    Field(
        description="Propriétés à renvoyer, ex. ['typeActe', 'inclusionNote'] "
        "(facultatif ; par défaut, toutes les propriétés du concept)"
    ),
]
TextFilter = Annotated[
    str | None,
    Field(description="Filtre textuel sur les libellés, ex. 'asthme' (facultatif)"),
]
Offset = Annotated[
    int, Field(ge=0, description="Index du premier résultat (pagination)")
]
SummaryOnly = Annotated[
    bool,
    Field(description="Si vrai, renvoie seulement le nombre total, sans les concepts"),
]
Search = Annotated[
    str | None,
    Field(
        description="Mots à rechercher dans le titre, le nom ou l'URL (casse et "
        "accents ignorés), ex. 'cim 10', 'loinc', 'specialite'"
    ),
]
Status = Annotated[
    PublicationStatus | None,
    Field(
        description="Statut de publication : 'active', 'draft' (brouillon), "
        "'retired' (retiré) ou 'unknown' (facultatif ; par défaut, tous les statuts)"
    ),
]


Count20 = Annotated[
    int, Field(ge=1, description="Nombre maximal de résultats (défaut : 20)")
]
Count50 = Annotated[
    int, Field(ge=1, description="Nombre maximal de résultats (défaut : 50)")
]


def create_mcp_app(
    *,
    runtime: ServerRuntime | None = None,
    guards: QueryGuards | None = None,
) -> MCPServer:
    """Construit le serveur MCP. `runtime` et `guards` permettent aux tests d'injecter
    un client simulé et des seuils réduits. En production, ils sont omis : le serveur
    interroge toujours le SMT avec les valeurs de config.py.
    """
    runtime = runtime or ServerRuntime()
    guards = guards or QueryGuards()
    atexit.register(runtime.close)
    logger.info("Serveur FHIR : %s", config.FHIR_BASE_URL)

    mcp = MCPServer(
        "serveur-mcp-api-fhir-smt",
        instructions=INSTRUCTIONS,
        version=config.VERSION,
        cache_hints=_CACHE_HINTS,
    )

    def _backend(
        tool: str,
        args: dict[str, Any],
        call: Callable[[], dict[str, Any]],
        *,
        cacheable: bool = True,
    ) -> dict[str, Any]:
        """Appel au SMT encadré par les garde-fous communs.

        Cache des réponses, puis coupe-circuit et budget de débit, plafond de
        concurrence, traduction des erreurs, et enfin enregistrement du succès ou
        de l'échec pour le coupe-circuit.
        """
        key = json.dumps([tool, args], sort_keys=True, default=str)
        if cacheable and (hit := guards.cached(key)) is not None:
            return hit

        def observed() -> dict[str, Any]:
            try:
                result = call()
            except HttpRequestError as exc:
                guards.record_failure(exc.status_code, exc.retry_after)
                if guards.backend_blocked:
                    # Cet échec a déclenché le blocage : on prévient dès maintenant
                    raise guards.blocked_error() from exc
                raise
            except Exception:
                # Échec sans réponse exploitable du SMT (argument invalide…) : un
                # éventuel appel test doit pouvoir être retenté
                guards.release_probe()
                raise
            guards.record_success()
            return result

        guards.before_backend_call()
        with guards.concurrency:
            result = _tool_guard(observed)
        if cacheable:
            guards.store(key, result)
        return result

    # ── Serveur ─────────────────────────────────────────────────────────

    @mcp.tool(
        title="État du serveur",
        description=(
            "Vérifie que le serveur FHIR répond et renvoie sa version FHIR et les "
            "opérations qu'il déclare.\n\n"
            "QUAND L'UTILISER : en cas d'erreurs répétées, ou pour savoir si une "
            "opération est prise en charge."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_server_health() -> dict[str, Any]:
        return _backend("server_health", {}, runtime.server_health, cacheable=False)

    # ── Catalogue ───────────────────────────────────────────────────────

    def _list(
        resource_type: ResourceType,
        *,
        search: str | None,
        status: PublicationStatus | None,
        offset: int,
        count: int,
        source: str | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "query": search,
            "source": source,
            "target": target,
            "status": status,
            "offset": offset,
            "count": guards.clamp_page_size(count),
        }
        return _backend(
            f"list:{resource_type}",
            args,
            lambda: runtime.list_resources(resource_type, **args),
        )

    @mcp.tool(
        title="Lister les terminologies",
        description=(
            "Recherche les terminologies (CodeSystem) publiées sur le serveur et "
            "renvoie leur URL canonique, leur version, leur titre et leur nombre de "
            "concepts.\n\n"
            "QUAND L'UTILISER : toujours en premier, pour obtenir l'URL `system` "
            "attendue par les autres outils. Exemples de recherche : 'cim 10', 'ccam', "
            "'loinc', 'snomed', 'atc', 'specialite'. Une terminologie peut apparaître "
            "en plusieurs versions ou éditions (SNOMED CT : édition internationale et "
            "module français)."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_list_terminologies(
        search: Search = None,
        status: Status = None,
        offset: Offset = 0,
        count: Count20 = 20,
    ) -> dict[str, Any]:
        return _list(
            "CodeSystem", search=search, status=status, offset=offset, count=count
        )

    @mcp.tool(
        title="Lister les jeux de valeurs",
        description=(
            "Recherche les jeux de valeurs (ValueSet) publiés sur le serveur, par "
            "exemple les JDV du NOS. Renvoie leur URL canonique, à passer en "
            "`value_set_url` à smt_expand ou smt_validate_code. `total` donne le "
            "nombre de résultats : combiné à `status`, il permet de compter par "
            "exemple les jeux de valeurs actifs en un seul appel (count=1)."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_list_value_sets(
        search: Search = None,
        status: Status = None,
        offset: Offset = 0,
        count: Count20 = 20,
    ) -> dict[str, Any]:
        return _list(
            "ValueSet", search=search, status=status, offset=offset, count=count
        )

    @mcp.tool(
        title="Lister les alignements",
        description=(
            "Recherche les alignements entre terminologies (ConceptMap) publiés sur le "
            "serveur. `source` et `target` filtrent sur l'URL exacte de la "
            "terminologie ou du jeu de valeurs source ou cible. Renvoie l'URL à passer "
            "en concept_map_url à smt_translate."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_list_concept_maps(
        search: Search = None,
        source: Annotated[str | None, Field(description="URL source exacte")] = None,
        target: Annotated[str | None, Field(description="URL cible exacte")] = None,
        status: Status = None,
        offset: Offset = 0,
        count: Count20 = 20,
    ) -> dict[str, Any]:
        return _list(
            "ConceptMap",
            search=search,
            source=source,
            target=target,
            status=status,
            offset=offset,
            count=count,
        )

    # ── Opérations sur les codes ────────────────────────────────────────

    @mcp.tool(
        title="Consulter un code",
        description=(
            "Détail d'un code via CodeSystem/$lookup : libellé préféré, désignations "
            "(synonymes, langues), propriétés (parents, enfants, statut actif ou "
            "inactif, notes d'inclusion, type d'acte CCAM…). Toutes les propriétés "
            "sont renvoyées, sauf si `properties` en restreint la liste.\n\n"
            "QUAND L'UTILISER : vérifier ce que désigne un code connu, afficher ses "
            "synonymes, savoir s'il est inactif. Si le code n'existe pas, renvoie "
            "found=false avec un message (pas une erreur).\n\n"
            "PAS POUR : rechercher par libellé (smt_expand avec filter) ni "
            "consulter une liste de codes un par un (un expand suffit)."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_lookup(
        system: System,
        code: Code,
        version: Version = None,
        properties: Properties = None,
        display_language: DisplayLanguage = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "system": system,
            "code": code,
            "version": version,
            "properties": properties,
            "display_language": display_language,
        }
        return _backend("lookup", args, lambda: runtime.lookup(**args))

    @mcp.tool(
        title="Valider un code",
        description=(
            "Vérifie qu'un code existe dans une terminologie (CodeSystem/"
            "$validate-code) ou appartient à un jeu de valeurs si value_set_url est "
            "fourni (ValueSet/$validate-code). Avec `display`, vérifie aussi le "
            "libellé. Renvoie result=true/false, le libellé officiel et un message "
            "explicatif.\n\n"
            "QUAND L'UTILISER : confirmer la présence d'un code ou un couple "
            "code/libellé dans une Terminologie ou un jeu de valeurs, contrôler un "
            "couple code/libellé avant de l'utiliser."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_validate_code(
        system: System,
        code: Code,
        display: Annotated[
            str | None, Field(description="Libellé à vérifier (facultatif)")
        ] = None,
        value_set_url: Annotated[
            str | None,
            Field(description="URL d'un jeu de valeurs auquel le code doit appartenir"),
        ] = None,
        version: Version = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "system": system,
            "code": code,
            "version": version,
            "display": display,
            "value_set_url": value_set_url,
        }
        return _backend("validate_code", args, lambda: runtime.validate_code(**args))

    @mcp.tool(
        title="Tester une relation hiérarchique",
        description=(
            "Indique la relation entre deux codes d'une même terminologie "
            "(CodeSystem/$subsumes) : equivalent, subsumes (A englobe B), subsumed-by "
            "(A est englobé par B) ou not-subsumed.\n\n"
            "QUAND L'UTILISER : savoir si un concept est un cas particulier d'un autre."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_subsumes(
        system: System,
        code_a: Code,
        code_b: Code,
        version: Version = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "system": system,
            "code_a": code_a,
            "code_b": code_b,
            "version": version,
        }
        return _backend("subsumes", args, lambda: runtime.subsumes(**args))

    @mcp.tool(
        title="Traduire un code",
        description=(
            "Traduit un code vers une autre terminologie à l'aide d'un alignement "
            "(ConceptMap/$translate). Indiquez concept_map_url (voir "
            "smt_list_concept_maps) ou target_system. reverse=true traduit de la cible "
            "vers la source. Renvoie les correspondances avec leur équivalence "
            "(equivalent, wider, narrower…)."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_translate(
        system: System,
        code: Code,
        concept_map_url: Annotated[
            str | None, Field(description="URL canonique de l'alignement (ConceptMap)")
        ] = None,
        target_system: Annotated[
            str | None, Field(description="URL de la terminologie cible")
        ] = None,
        reverse: Annotated[
            bool, Field(description="Traduire de la cible vers la source")
        ] = False,
        version: Version = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "system": system,
            "code": code,
            "version": version,
            "concept_map_url": concept_map_url,
            "target_system": target_system,
            "reverse": reverse,
        }
        return _backend("translate", args, lambda: runtime.translate(**args))

    @mcp.tool(
        title="Inactivation d'un concept SNOMED CT",
        description=(
            "Pour un concept SNOMED CT inactif, renvoie la raison de son inactivation "
            "(au format « code |libellé| ») et ses remplacements, issus des "
            "associations historiques REPLACED BY, SAME AS, POSSIBLY EQUIVALENT TO et "
            "ALTERNATIVE. Si le serveur ne fournit pas la raison, inactivation_reason "
            "vaut « Raison d'inactivation non gérée en FHIR ». Un concept actif ou "
            "inconnu renvoie une erreur explicite. Si le serveur ne fournit pas de "
            "remplacement, indique que soit il n'existe pas de remplacement soit ce "
            "remplacement n'est pas encore géré par FHIR.\n\n"
            "QUAND L'UTILISER : un code SNOMED CT issu de données existantes est "
            "inactif (smt_lookup indique inactive=true) et il faut savoir "
            "pourquoi, et par quoi le remplacer.\n\n"
            "PAS POUR : les autres terminologies, ni les concepts actifs "
            "(smt_lookup)."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_snomed_get_inactivation(
        code: Annotated[
            str, Field(description="Identifiant du concept SNOMED CT (SCTID)")
        ],
        version: Version = None,
        display_language: DisplayLanguage = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "code": code,
            "version": version,
            "display_language": display_language,
        }
        return _backend(
            "snomed_inactivation", args, lambda: runtime.snomed_inactivation(**args)
        )

    # ── Expand ──────────────────────────────────────────────────────

    @mcp.tool(
        title="Étendre une terminologie ou un jeu de valeurs",
        description=(
            "Liste les concepts d'une terminologie entière (`system`) ou d'un jeu de "
            "valeurs publié (`value_set_url`), avec un filtre textuel facultatif "
            "(ValueSet/$expand). C'est l'outil de RECHERCHE PAR LIBELLÉ : system + "
            "filter='asthme'.\n\n"
            "QUAND L'UTILISER : retrouver un code à partir d'un libellé, lister les "
            "codes d'un jeu de valeurs, compter les concepts (summary_only=true).\n\n"
            "SNOMED CT : value_set_url accepte aussi les jeux de valeurs implicites, "
            "dont les expressions ECL : "
            "'http://snomed.info/sct/11000315107?fhir_vs=ecl/<<195967001' (édition "
            "française). Ces URL implicites ne fonctionnent pas pour les autres "
            "terminologies : utilisez `system`.\n\n"
            "RÈGLES : comptez d'abord avec summary_only=true si le résultat peut être "
            "large ; si le total dépasse 1000, demandez à l'utilisateur avant de "
            "paginer ; ne parcourez jamais toutes les pages de vous-même. La"
            f"pagination est limitée aux {config.MAX_PAGINATION_DEPTH} premiers résultats : "
            "au-delà, affinez la demande."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_expand(
        system: Annotated[
            str | None,
            Field(description="URL canonique de la terminologie à étendre"),
        ] = None,
        value_set_url: Annotated[
            str | None,
            Field(description="URL canonique d'un jeu de valeurs (ValueSet)"),
        ] = None,
        filter: TextFilter = None,
        version: Version = None,
        offset: Offset = 0,
        count: Count20 = 20,
        summary_only: SummaryOnly = False,
        display_language: DisplayLanguage = None,
    ) -> dict[str, Any]:
        if not system and not value_set_url:
            raise ToolError(
                "[E_INVALID_ARGUMENT] Indiquez `system` (terminologie, voir "
                "smt_list_terminologies) ou `value_set_url` (jeu de valeurs, voir "
                "smt_list_value_sets)."
            )
        if value_set_url:
            guards.check_ecl(value_set_url)
        guards.check_pagination_depth(offset)
        args: dict[str, Any] = {
            "system": None if value_set_url else system,
            "value_set_url": value_set_url,
            "version": version,
            "filter": filter,
            "offset": offset,
            "count": guards.clamp_page_size(count),
            "summary_only": summary_only,
            "display_language": display_language,
        }
        return _backend(
            "expand",
            args,
            lambda: guards.add_large_result_warning(
                runtime.expand(**args), summary_only
            ),
        )

    # ── Hiérarchie ──────────────────────────────────────────────────────

    def _hierarchy_tool(
        relation: str,
        *,
        system: str,
        code: str,
        version: str | None,
        filter: str | None,
        offset: int,
        count: int,
        summary_only: bool,
        display_language: str | None,
    ) -> dict[str, Any]:
        # Seuls les outils « un niveau » sont suivis : descendants et ancêtres sont
        # justement les alternatives en un appel
        if relation == "children":
            guards.check_traversal("down", system, code)
        elif relation == "parents":
            guards.check_traversal("up", system, code)
        guards.check_pagination_depth(offset)
        args: dict[str, Any] = {
            "system": system,
            "code": code,
            "version": version,
            "filter": filter,
            "offset": offset,
            "count": guards.clamp_page_size(count),
            "summary_only": summary_only,
            "display_language": display_language,
        }
        return _backend(
            f"hierarchy:{relation}",
            args,
            lambda: guards.add_large_result_warning(
                runtime.hierarchy(relation, **args), summary_only
            ),
        )

    @mcp.tool(
        title="Parents directs",
        description=(
            "Renvoie les parents directs d'un code (niveau immédiatement supérieur "
            "dans la hiérarchie), avec leurs libellés. Fonctionne pour toute "
            "terminologie hiérarchique (CIM-10, SNOMED CT, CCAM…).\n\n"
            "PAS POUR : remonter toute la chaîne (smt_get_ancestors en un seul "
            "appel) — ne l'appelez jamais en boucle sur chaque parent."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_get_parents(
        system: System,
        code: Code,
        version: Version = None,
        display_language: DisplayLanguage = None,
    ) -> dict[str, Any]:
        return _hierarchy_tool(
            "parents",
            system=system,
            code=code,
            version=version,
            filter=None,
            offset=0,
            count=50,
            summary_only=False,
            display_language=display_language,
        )

    @mcp.tool(
        title="Enfants directs",
        description=(
            "Renvoie les enfants directs d'un code (un niveau en dessous), avec leurs "
            "libellés.\n\n"
            "QUAND L'UTILISER : voir comment un concept se subdivise.\n\n"
            "PAS POUR : obtenir toute la sous-hiérarchie (smt_get_descendants "
            "en un seul appel) — ne l'appelez jamais en boucle sur chaque enfant."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_get_children(
        system: System,
        code: Code,
        version: Version = None,
        filter: TextFilter = None,
        offset: Offset = 0,
        count: Count50 = 50,
        display_language: DisplayLanguage = None,
    ) -> dict[str, Any]:
        return _hierarchy_tool(
            "children",
            system=system,
            code=code,
            version=version,
            filter=filter,
            offset=offset,
            count=count,
            summary_only=False,
            display_language=display_language,
        )

    @mcp.tool(
        title="Ancêtres",
        description=(
            "Renvoie tous les ancêtres d'un code (parents, grands-parents… jusqu'à la "
            "racine), sans le code lui-même, en un seul appel.\n\n"
            "QUAND L'UTILISER : situer un concept dans sa classification."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_get_ancestors(
        system: System,
        code: Code,
        version: Version = None,
        offset: Offset = 0,
        count: Count50 = 50,
        display_language: DisplayLanguage = None,
    ) -> dict[str, Any]:
        return _hierarchy_tool(
            "ancestors",
            system=system,
            code=code,
            version=version,
            filter=None,
            offset=offset,
            count=count,
            summary_only=False,
            display_language=display_language,
        )

    @mcp.tool(
        title="Descendants",
        description=(
            "Renvoie tous les descendants d'un code (enfants, petits-enfants…), sans "
            "le code lui-même, avec un filtre textuel facultatif.\n\n"
            "QUAND L'UTILISER : obtenir toute une sous-hiérarchie en un seul appel. "
            "Commencez par summary_only=true pour connaître la taille ; si elle "
            "dépasse 1000, demandez à l'utilisateur avant de paginer. La pagination "
            f"est limitée aux {config.MAX_PAGINATION_DEPTH} premiers résultats."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def smt_get_descendants(
        system: System,
        code: Code,
        version: Version = None,
        filter: TextFilter = None,
        offset: Offset = 0,
        count: Count50 = 50,
        summary_only: SummaryOnly = False,
        display_language: DisplayLanguage = None,
    ) -> dict[str, Any]:
        return _hierarchy_tool(
            "descendants",
            system=system,
            code=code,
            version=version,
            filter=filter,
            offset=offset,
            count=count,
            summary_only=summary_only,
            display_language=display_language,
        )

    return mcp


# ── Traitement des réponses et des erreurs ──────────────────────────────


def _truncate_response(result: dict[str, Any]) -> dict[str, Any]:
    """Tronque une réponse qui dépasse le budget de caractères.

    Raccourcit d'abord la plus grande liste par la fin, puis remplace les plus
    gros dictionnaires, et ajoute un avis de troncature.
    """
    serialised = json.dumps(result, default=str, ensure_ascii=False)
    if len(serialised) <= config.MAX_RESPONSE_CHARS:
        return result

    logger.warning(
        "Réponse de %d caractères tronquée (limite %d)",
        len(serialised),
        config.MAX_RESPONSE_CHARS,
    )
    trimmed = dict(result)
    list_fields = [(k, v) for k, v in trimmed.items() if isinstance(v, list) and v]
    dict_fields = [(k, v) for k, v in trimmed.items() if isinstance(v, dict) and v]
    if not list_fields and not dict_fields:
        return trimmed

    trimmed["_truncated"] = True
    trimmed["_truncation_notice"] = (
        "Réponse tronquée pour respecter la limite de taille. Affinez la demande "
        "(filtre, count, offset) pour obtenir un résultat complet."
    )

    def size() -> int:
        return len(json.dumps(trimmed, default=str, ensure_ascii=False))

    if list_fields:
        key = max(list_fields, key=lambda kv: len(json.dumps(kv[1], default=str)))[0]
        items = list(trimmed[key])
        trimmed[key] = items
        avg_item = max(
            1, len(json.dumps(items, default=str, ensure_ascii=False)) // len(items)
        )
        while items and (current := size()) > config.MAX_RESPONSE_CHARS:
            drop = min(
                len(items), max(1, (current - config.MAX_RESPONSE_CHARS) // avg_item)
            )
            del items[-drop:]
        if "returned" in trimmed:
            trimmed["returned"] = len(items)

    removed: set[str] = set()
    while size() > config.MAX_RESPONSE_CHARS:
        candidates = [
            (k, v)
            for k, v in trimmed.items()
            if isinstance(v, dict) and v and k not in removed
        ]
        if not candidates:
            break
        key = max(candidates, key=lambda kv: len(json.dumps(kv[1], default=str)))[0]
        trimmed[key] = {"_removed": "Champ retiré pour respecter la limite de taille."}
        removed.add(key)
    return trimmed


def _log_tool_error(
    code: str, exc: Exception, *, status_code: int | None = None, exc_info: bool = False
) -> None:
    logger.error(
        "Échec de l'outil [%s] : %s",
        code,
        exc,
        exc_info=exc_info,
        extra={
            "error_code": code,
            "error_type": type(exc).__name__,
            "status_code": status_code,
        },
    )


_HINT_BAD_REQUEST = (
    " Vérifiez les paramètres : l'URL canonique doit provenir de smt_list_terminologies, "
    "smt_list_value_sets ou smt_list_concept_maps."
)


def _tool_guard(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Exécute l'appel et traduit les échecs prévus en `ToolError` codées.

    Le SDK ne montre au client que le message d'une `ToolError` ; toute autre
    exception devient un « Error executing tool » sans détail.
    """
    try:
        return _truncate_response(fn())
    except GuardError as exc:
        logger.warning("Appel bloqué par un garde-fou : %s", exc)
        raise
    except ToolError:
        raise
    except HttpRequestError as exc:
        message = str(exc)
        if message.startswith("Délai dépassé"):
            code = "E_BACKEND_TIMEOUT"
        elif exc.status_code in {429, 503}:
            code = "E_BACKEND_UNAVAILABLE"
            message += (
                " Le SMT est surchargé ou indisponible : les appels sont suspendus "
                "quelques instants. Réessayez plus tard, sans répéter l'appel en "
                "boucle."
            )
        elif exc.status_code in {401, 403}:
            code = "E_BACKEND_AUTH"
        elif exc.status_code is not None:
            code = "E_BACKEND_HTTP"
        else:
            code = "E_BACKEND_REQUEST"
        if exc.status_code in {400, 404, 422}:
            message += _HINT_BAD_REQUEST
        _log_tool_error(code, exc, status_code=exc.status_code)
        raise ToolError(f"[{code}] {message}") from exc
    except ValueError as exc:
        _log_tool_error("E_INVALID_ARGUMENT", exc)
        raise ToolError(f"[E_INVALID_ARGUMENT] {exc}") from exc
    except Exception as exc:
        _log_tool_error("E_INTERNAL", exc, exc_info=True)
        raise
