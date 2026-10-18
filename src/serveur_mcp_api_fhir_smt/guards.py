"""Garde-fous appliqués avant et après les appels à l'API FHIR du SMT.

Protections, dans l'ordre où un appel les rencontre :
  1. Détection des parcours niveau par niveau — enfants des enfants, ou parents
     des parents, au lieu d'une seule requête de descendants ou d'ancêtres
  2. Profondeur de pagination — `offset` limité aux premiers résultats
     (MAX_PAGINATION_DEPTH)
  3. Taille de page — nombre de concepts par appel ramené au plafond
     (MAX_PAGE_SIZE)
  4. Cache des réponses — un appel identique récent n'interroge pas le SMT
  5. Coupe-circuit — suspend les appels quand le SMT est surchargé (429, 503)
     ou défaillant (erreurs 5xx ou délais dépassés répétés), avec un appel test
     et des pauses allongées ; bloque les appels si la panne dure
  6. Limitation de débit — fenêtre glissante, pour tout le processus
  7. Plafond de concurrence — empêche les rafales d'appels parallèles
  8. Avertissement sur résultat volumineux — signale un total au-delà d'un seuil
     (LARGE_RESULT_WARNING_THRESHOLD), sans bloquer

En stdio, chaque utilisateur lance son propre processus : ces limites s'appliquent
par utilisateur. Elles protègent le SMT des dérives d'un assistant, pas d'une
charge collective, qui relève du SMT lui-même.

Chaque garde-fou produit un message en français, sans détail interne, qui dit au
modèle comment reformuler sa demande.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from time import monotonic
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from . import config

logger = logging.getLogger(__name__)

Direction = Literal["down", "up"]


# ── Exceptions ──────────────────────────────────────────────────────────


class GuardError(ToolError):
    """Appel bloqué par un garde-fou.

    Le message peut être renvoyé tel quel au modèle. C'est une `ToolError` car le
    SDK ne montre au client que le message d'une `ToolError`.
    """


class RateLimitError(GuardError):
    """Limite de débit atteinte."""


class BackendUnavailableError(GuardError):
    """Appel suspendu par le coupe-circuit."""


class BackendBlockedError(GuardError):
    """Appels bloqués jusqu'au redémarrage : le SMT ne répond plus."""


# ── 1. Parcours niveau par niveau ───────────────────────────────────────


_TRAVERSAL_HINTS: dict[Direction, tuple[str, str, str]] = {
    # sens : (outil appelé en boucle, alternative, description)
    "down": (
        "smt_get_children",
        "smt_get_descendants",
        "les enfants de concepts successifs",
    ),
    "up": (
        "smt_get_parents",
        "smt_get_ancestors",
        "les parents de concepts successifs",
    ),
}


class TraversalTracker:
    """Détecte la navigation niveau par niveau dans une hiérarchie.

    Compte, pour chaque sens (descente avec les enfants, remontée avec les
    parents), les codes *distincts* demandés sur une fenêtre glissante de 60 s.
    Redemander le même code ne compte pas. Les outils qui renvoient toute une
    sous-hiérarchie ou toute la chaîne d'ancêtres en un appel ne sont pas suivis :
    ce sont les alternatives recommandées.
    """

    def __init__(
        self,
        max_steps_per_minute: int = config.MAX_TRAVERSAL_STEPS_PER_MINUTE,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.max_steps_per_minute = max_steps_per_minute
        self._clock = clock
        self._steps: dict[Direction, deque[tuple[float, tuple[str, str]]]] = {
            "down": deque(),
            "up": deque(),
        }
        self._lock = threading.Lock()

    def check(self, direction: Direction, system: str, code: str) -> None:
        with self._lock:
            now = self._clock()
            steps = self._steps[direction]
            while steps and steps[0][0] <= now - 60:
                steps.popleft()
            key = (system, code)
            seen = {k for _, k in steps}
            if key in seen:
                return
            if len(seen) >= self.max_steps_per_minute:
                tool, alternative, what = _TRAVERSAL_HINTS[direction]
                logger.warning(
                    "Parcours niveau par niveau détecté (%s) : %d codes en 60 s",
                    direction,
                    len(seen) + 1,
                )
                raise GuardError(
                    f"[E_RECURSIVE_TRAVERSAL] {tool} a été appelé pour {len(seen) + 1} "
                    f"codes différents en 60 s : vous semblez parcourir {what}, niveau "
                    f"par niveau. Utilisez plutôt {alternative} (code='{code}'), qui "
                    "renvoie le résultat complet en un seul appel."
                )
            steps.append((now, key))


# ── 2. Profondeur de pagination ─────────────────────────────────────────


def check_pagination_depth(offset: int, max_pagination_depth: int) -> None:
    """Refuse de paginer au-delà des `max_pagination_depth` premiers résultats."""
    if offset >= max_pagination_depth:
        logger.warning(
            "Pagination refusée : offset=%d (borne %d)", offset, max_pagination_depth
        )
        raise GuardError(
            "[E_PAGINATION_DEPTH_EXCEEDED] La pagination est limitée aux "
            f"{_number(max_pagination_depth)} premiers résultats. Ne parcourez pas un "
            "grand ensemble page par page : affinez la demande (filtre textuel, "
            "concept plus spécifique, jeu de valeurs plus précis) ou indiquez le total "
            "à l'utilisateur."
        )


# ── 3. Taille de page ───────────────────────────────────────────────────


def clamp_page_size(requested: int, max_page_size: int) -> int:
    """Ramène le nombre de concepts demandés dans [1, max_page_size]."""
    capped = max(1, min(requested, max_page_size))
    if capped != requested:
        logger.info("Nombre plafonné de %d à %d", requested, capped)
    return capped


# ── 4. Cache des réponses ───────────────────────────────────────────────


class ResponseCache:
    """Cache à durée de vie des réponses d'outils, borné en nombre d'entrées.

    Les assistants refont souvent les mêmes appels : une réponse identique récente
    est resservie sans interroger le SMT ni consommer de budget de débit. Les
    valeurs sont copiées à l'entrée et à la sortie pour rester inaltérables.
    """

    def __init__(
        self,
        ttl_seconds: int = config.RESPONSE_CACHE_TTL_SECONDS,
        max_entries: int = config.RESPONSE_CACHE_MAX_ENTRIES,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if self._clock() - stored_at > self._ttl:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return copy.deepcopy(value)

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), copy.deepcopy(value))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


# ── 5. Coupe-circuit ────────────────────────────────────────────────────


class CircuitBreaker:
    """Suspend les appels au SMT quand il est surchargé ou défaillant.

    Ouverture :
    - 429 (trop de requêtes) ou 503 (indisponible) : immédiate, pour la durée de
      l'en-tête `Retry-After` si le serveur la donne ;
    - erreurs 5xx, délais dépassés ou échecs réseau : après `failure_threshold`
      échecs consécutifs.

    À la fin de chaque suspension, un seul appel test est envoyé au SMT ; les
    autres sont refusés jusqu'à son résultat. S'il réussit, le coupe-circuit se
    referme ; s'il échoue, il se rouvre pour une durée doublée (30, 60, 120 s…,
    plafonnée à `max_cooldown_seconds`).

    Si aucun appel n'a réussi `block_after_seconds` après le début de la panne
    (première ouverture), un dernier appel test est tenté ; s'il échoue, les appels
    sont bloqués jusqu'au redémarrage du serveur.

    Les erreurs 4xx (code ou URL inconnus…) ne sont pas des pannes : le serveur a
    répondu, elles comptent comme une réussite.
    """

    def __init__(
        self,
        failure_threshold: int = config.BACKEND_FAILURE_THRESHOLD,
        cooldown_seconds: float = config.BACKEND_COOLDOWN_SECONDS,
        max_cooldown_seconds: float = config.BACKEND_MAX_COOLDOWN_SECONDS,
        block_after_seconds: float = config.BACKEND_BLOCK_AFTER_SECONDS,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self.block_after_seconds = block_after_seconds
        self._clock = clock
        self._failures = 0
        # Fin de la suspension en cours ; None quand le coupe-circuit est fermé
        self._open_until: float | None = None
        # Nombre d'ouvertures depuis la dernière réussite (allongement progressif)
        self._openings = 0
        self._outage_since: float | None = None
        self._probe_in_flight = False
        self._blocked = False
        self._lock = threading.Lock()

    @property
    def blocked(self) -> bool:
        return self._blocked

    def blocked_error(self) -> BackendBlockedError:
        minutes = int(self.block_after_seconds // 60)
        return BackendBlockedError(
            f"[E_BACKEND_BLOCKED] Le SMT n'a pas répondu correctement depuis plus de "
            f"{minutes} minutes : les appels au SMT sont désormais bloqués. Indiquez "
            "à l'utilisateur que les services du SMT sont indisponibles et qu'il "
            "devra redémarrer son outil IA (Claude Desktop, Claude Code, VS Code…) "
            "une fois ces services rétablis. Ne réessayez pas l'appel."
        )

    def check(self) -> None:
        """Autorise l'appel, en fait l'appel test, ou lève une erreur de suspension."""
        with self._lock:
            if self._blocked:
                raise self.blocked_error()
            if self._open_until is None:
                return
            now = self._clock()
            remaining = self._open_until - now
            deadline_reached = (
                self._outage_since is not None
                and now - self._outage_since >= self.block_after_seconds
            )
            # À l'échéance, on n'attend pas la fin de la suspension : dernier test
            if remaining > 0 and not deadline_reached:
                raise BackendUnavailableError(
                    "[E_BACKEND_UNAVAILABLE] Le SMT est momentanément surchargé ou "
                    f"indisponible : aucune requête n'est envoyée pendant encore "
                    f"~{int(remaining) + 1} s. Indiquez-le à l'utilisateur et "
                    "réessayez plus tard, sans répéter l'appel en boucle."
                )
            if self._probe_in_flight:
                raise BackendUnavailableError(
                    "[E_BACKEND_UNAVAILABLE] Le SMT est en cours de vérification après "
                    "une panne : réessayez dans quelques secondes, sans répéter "
                    "l'appel en boucle."
                )
            self._probe_in_flight = True

    def release_probe(self) -> None:
        """Libère l'appel test s'il n'a pas pu interroger le SMT (débit, argument…)."""
        with self._lock:
            self._probe_in_flight = False

    def record_success(self) -> None:
        with self._lock:
            if self._blocked:
                return  # le blocage est définitif jusqu'au redémarrage
            self._failures = 0
            self._open_until = None
            self._openings = 0
            self._outage_since = None
            self._probe_in_flight = False

    def record_failure(
        self, status_code: int | None, retry_after: float | None
    ) -> None:
        if status_code is not None and status_code < 500 and status_code != 429:
            self.record_success()  # le serveur a répondu
            return
        with self._lock:
            if self._blocked:
                return
            now = self._clock()
            if self._open_until is not None:
                if not self._probe_in_flight:
                    return  # requête partie avant l'ouverture : déjà prise en compte
                self._probe_in_flight = False
                if (
                    self._outage_since is not None
                    and now - self._outage_since >= self.block_after_seconds
                ):
                    self._blocked = True
                    logger.error(
                        "Coupe-circuit : aucun succès depuis %.0f s, appels au SMT "
                        "bloqués jusqu'au redémarrage",
                        now - self._outage_since,
                    )
                    return
                self._open(retry_after, now)
            elif status_code in (429, 503):
                self._open(retry_after, now)
            else:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._open(None, now)

    def _open(self, retry_after: float | None, now: float) -> None:
        backoff = self.cooldown_seconds * 2**self._openings
        seconds = retry_after if retry_after is not None else backoff
        seconds = max(1.0, min(seconds, self.max_cooldown_seconds))
        if self._outage_since is None:
            self._outage_since = now
        self._open_until = now + seconds
        self._openings += 1
        self._failures = 0
        logger.warning("Coupe-circuit ouvert pour %.0f s", seconds)


# ── 6. Limitation de débit ──────────────────────────────────────────────


class RollingRateLimiter:
    """Limiteur à fenêtre glissante, une instance par processus."""

    def __init__(
        self,
        max_calls: int = config.RATE_LIMIT_CALLS,
        window_seconds: int = config.RATE_LIMIT_WINDOW_SECONDS,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._clock = clock
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            now = self._clock()
            while self._timestamps and self._timestamps[0] <= now - self.window_seconds:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.max_calls:
                retry_in = int(self.window_seconds - (now - self._timestamps[0])) + 1
                logger.warning(
                    "Limite de débit atteinte : %d appels en %d s",
                    self.max_calls,
                    self.window_seconds,
                )
                raise RateLimitError(
                    f"[E_RATE_LIMIT] Limite atteinte : {self.max_calls} requêtes par "
                    f"{self.window_seconds} s au maximum. Réessayez dans ~{retry_in}s. "
                    "Pour économiser des requêtes : commencez par summary_only=true, "
                    "ne demandez la page suivante que si l'utilisateur le souhaite, et "
                    "préférez une seule expansion à plusieurs consultations de codes."
                )
            self._timestamps.append(now)


# ── 7. Plafond de concurrence ───────────────────────────────────────────


class ConcurrencyLimiter:
    """Sémaphore limitant le nombre de requêtes simultanées vers le serveur."""

    def __init__(self, max_concurrent: int = config.MAX_CONCURRENT_REQUESTS) -> None:
        self._semaphore = threading.Semaphore(max_concurrent)

    def __enter__(self) -> ConcurrencyLimiter:
        self._semaphore.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self._semaphore.release()


# ── 8. Avertissement sur résultat volumineux ────────────────────────────


def add_large_result_warning(
    result: dict[str, Any], threshold: int, summary_only: bool
) -> dict[str, Any]:
    """Ajoute un avertissement si le total dépasse le seuil (hors appel de résumé)."""
    total = result.get("total")
    if isinstance(total, int) and total > threshold and not summary_only:
        result["_large_result_warning"] = (
            f"Résultat volumineux ({total} concepts) : seuls les "
            f"{result.get('returned', 0)} premiers sont renvoyés. Ne parcourez pas les "
            "pages suivantes sans l'accord de l'utilisateur. Proposez plutôt d'affiner "
            "(filtre textuel, sous-partie de la hiérarchie, ValueSet plus précis)."
        )
        logger.info("Avertissement sur résultat volumineux : total=%d", total)
    return result


# ── Orchestrateur ───────────────────────────────────────────────────────


class QueryGuards:
    """Regroupe les garde-fous ; une instance par serveur, partagée par les outils.

    Les paramètres valent par défaut les constantes de `config.py` ; ils ne sont
    surchargés que dans les tests.
    """

    def __init__(
        self,
        *,
        rate_limit_calls: int = config.RATE_LIMIT_CALLS,
        rate_limit_window_seconds: int = config.RATE_LIMIT_WINDOW_SECONDS,
        max_concurrent_requests: int = config.MAX_CONCURRENT_REQUESTS,
        max_page_size: int = config.MAX_PAGE_SIZE,
        max_pagination_depth: int = config.MAX_PAGINATION_DEPTH,
        large_result_warning_threshold: int = config.LARGE_RESULT_WARNING_THRESHOLD,
        max_traversal_steps_per_minute: int = config.MAX_TRAVERSAL_STEPS_PER_MINUTE,  # noqa
        response_cache_ttl_seconds: int = config.RESPONSE_CACHE_TTL_SECONDS,
        response_cache_max_entries: int = config.RESPONSE_CACHE_MAX_ENTRIES,
        backend_failure_threshold: int = config.BACKEND_FAILURE_THRESHOLD,
        backend_cooldown_seconds: float = config.BACKEND_COOLDOWN_SECONDS,
        backend_block_after_seconds: float = config.BACKEND_BLOCK_AFTER_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.max_page_size = max_page_size
        self.max_pagination_depth = max_pagination_depth
        self.large_result_warning_threshold = large_result_warning_threshold
        self._traversal = TraversalTracker(max_traversal_steps_per_minute, clock=clock)
        self._cache = ResponseCache(
            response_cache_ttl_seconds, response_cache_max_entries, clock=clock
        )
        self._breaker = CircuitBreaker(
            backend_failure_threshold,
            backend_cooldown_seconds,
            block_after_seconds=backend_block_after_seconds,
            clock=clock,
        )
        self._rate_limiter = RollingRateLimiter(
            rate_limit_calls, rate_limit_window_seconds, clock=clock
        )
        self._concurrency = ConcurrencyLimiter(max_concurrent_requests)

    # Avant l'appel

    def check_traversal(self, direction: Direction, system: str, code: str) -> None:
        self._traversal.check(direction, system, code)

    def check_pagination_depth(self, offset: int) -> None:
        check_pagination_depth(offset, self.max_pagination_depth)

    def clamp_page_size(self, count: int) -> int:
        return clamp_page_size(count, self.max_page_size)

    def cached(self, key: str) -> dict[str, Any] | None:
        return self._cache.get(key)

    def before_backend_call(self) -> None:
        """Coupe-circuit puis budget de débit, juste avant d'interroger le SMT."""
        self._breaker.check()
        try:
            self._rate_limiter.check()
        except RateLimitError:
            self._breaker.release_probe()
            raise

    @property
    def concurrency(self) -> ConcurrencyLimiter:
        """Plafond de concurrence, à utiliser comme gestionnaire de contexte."""
        return self._concurrency

    # Après l'appel

    def record_success(self) -> None:
        self._breaker.record_success()

    def release_probe(self) -> None:
        """À appeler quand l'appel a échoué sans réponse exploitable du SMT."""
        self._breaker.release_probe()

    @property
    def backend_blocked(self) -> bool:
        return self._breaker.blocked

    def blocked_error(self) -> BackendBlockedError:
        return self._breaker.blocked_error()

    def record_failure(
        self, status_code: int | None, retry_after: float | None
    ) -> None:
        self._breaker.record_failure(status_code, retry_after)

    def store(self, key: str, result: dict[str, Any]) -> None:
        self._cache.set(key, result)

    def add_large_result_warning(
        self, result: dict[str, Any], summary_only: bool
    ) -> dict[str, Any]:
        """Avertissement si le total dépasse le seuil (n'empêche pas l'appel)."""
        return add_large_result_warning(
            result, self.large_result_warning_threshold, summary_only
        )


def _number(value: int) -> str:
    """Nombre avec séparateur de milliers à la française : 1 000."""
    return f"{value:,}".replace(",", " ")
