"""Paramètres du serveur, regroupés ici pour faciliter la maintenance.

Le serveur est dédié à l'API FHIR du SMT : ces valeurs ne sont pas modifiables par
les utilisateurs (ni fichier, ni variable d'environnement, ni option de ligne de
commande). Pour en changer une, modifiez ce fichier et publiez une nouvelle version.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    VERSION = _pkg_version("serveur-mcp-api-fhir-smt")
except PackageNotFoundError:  # pragma: no cover - arbre non installé
    VERSION = "0.0.0"

# ── Serveur FHIR ────────────────────────────────────────────────────────

FHIR_BASE_URL = "https://smt.esante.gouv.fr/fhir"
REQUEST_TIMEOUT_SECONDS = 15.0
USER_AGENT = f"serveur-mcp-api-fhir-smt/{VERSION}"

# Langue des libellés demandée au serveur (paramètre FHIR displayLanguage). None
# laisse le serveur renvoyer le libellé préféré de chaque terminologie : forcer
# « fr » peut faire choisir un synonyme plutôt que le libellé officiel (CIM-10).
DEFAULT_DISPLAY_LANGUAGE: str | None = None

# ── Catalogue (CodeSystem, ValueSet, ConceptMap) ────────────────────────

CATALOG_CACHE_TTL_SECONDS = 3600
CATALOG_PAGE_SIZE = 500
# Nombre maximal de pages suivies par chargement, par sécurité
CATALOG_MAX_PAGES = 50

# ── Garde-fous ──────────────────────────────────────────────────────────

# En stdio, chaque utilisateur lance son propre processus : ces limites
# s'appliquent par utilisateur.

# Limitation de débit sur fenêtre glissante (requêtes réellement envoyées au SMT ;
# les réponses servies par le cache ne comptent pas)
RATE_LIMIT_CALLS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
# Requêtes simultanées vers le serveur FHIR
MAX_CONCURRENT_REQUESTS = 3
# Taille de page : nombre maximal de concepts renvoyés par un appel (au-delà, la
# valeur demandée est ramenée à ce plafond, sans erreur)
MAX_PAGE_SIZE = 100
# Profondeur de pagination : `offset` maximal, exclu (au-delà, erreur
# E_PAGINATION_DEPTH_EXCEEDED) ; borne le nombre de concepts atteignables en
# paginant à MAX_PAGINATION_DEPTH + MAX_PAGE_SIZE
MAX_PAGINATION_DEPTH = 1000
# Avertissement sur résultat volumineux : au-delà de ce total (taille de
# l'ensemble complet, pas de la page), le champ `_large_result_warning` invite
# le modèle à ne pas paginer sans l'accord de l'utilisateur
LARGE_RESULT_WARNING_THRESHOLD = 1000
# Parcours niveau par niveau : codes distincts tolérés par minute et par sens
# (enfants des enfants, parents des parents)
MAX_TRAVERSAL_STEPS_PER_MINUTE = 10
# Cache des réponses d'outils
RESPONSE_CACHE_TTL_SECONDS = 600
RESPONSE_CACHE_MAX_ENTRIES = 1024
# Coupe-circuit : suspension après N échecs serveur consécutifs (5xx, délai
# dépassé), ou immédiatement sur 429/503 (durée de Retry-After si fournie)
BACKEND_FAILURE_THRESHOLD = 3
# Première suspension, doublée à chaque appel test en échec (30, 60, 120, 240 s…)
BACKEND_COOLDOWN_SECONDS = 30.0
BACKEND_MAX_COOLDOWN_SECONDS = 300.0
# Sans réussite ce délai après le début de la panne, les appels au SMT sont
# bloqués jusqu'au redémarrage du serveur (donc de l'outil IA de l'utilisateur)
BACKEND_BLOCK_AFTER_SECONDS = 300.0

# ── Réponses ────────────────────────────────────────────────────────────

# Taille maximale d'une réponse d'outil (~25 000 jetons à 3 caractères par jeton)
MAX_RESPONSE_CHARS = 75_000
