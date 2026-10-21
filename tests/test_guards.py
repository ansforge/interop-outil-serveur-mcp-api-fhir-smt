from __future__ import annotations

import threading
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from serveur_mcp_api_fhir_smt.guards import (
    BackendBlockedError,
    BackendUnavailableError,
    CircuitBreaker,
    ConcurrencyLimiter,
    GuardError,
    QueryGuards,
    RateLimitError,
    ResponseCache,
    RollingRateLimiter,
    TraversalTracker,
    add_large_result_warning,
    check_ecl,
    check_pagination_depth,
    clamp_page_size,
)

CIM10 = "https://smt.esante.gouv.fr/terminologie-cim-10"
SNOMED_FR_ECL = "http://snomed.info/sct/11000315107?fhir_vs=ecl/"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def test_guard_errors_are_tool_errors():
    # Le SDK ne transmet au client que le message d'une ToolError
    assert issubclass(GuardError, ToolError)
    assert issubclass(RateLimitError, GuardError)
    assert issubclass(BackendUnavailableError, GuardError)


# ── Filtrage ECL ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ecl",
    [
        "<<404684003",
        " << 138875005 ",
        "<71388002",
        "<<404684003:*=*",
        "<<105590001.127489000",
        "<<404684003:{{+HISTORY}}",
        "(<<64572001:116676008=<<49755003) MINUS (<<64572001:116676008=<<4147007)",
    ],
)
def test_check_ecl_blocks_expensive_patterns(ecl: str):
    with pytest.raises(GuardError, match=r"\[E_QUERY_BLOCKED\]"):
        check_ecl(SNOMED_FR_ECL + ecl)


def test_check_ecl_sees_through_escaping():
    with pytest.raises(GuardError, match=r"\[E_QUERY_BLOCKED\]"):
        check_ecl(SNOMED_FR_ECL + "&lt;&lt;404684003")
    with pytest.raises(GuardError, match=r"\[E_QUERY_BLOCKED\]"):
        check_ecl("http://snomed.info/sct?fhir_vs=ecl/%3C%3C404684003")


@pytest.mark.parametrize(
    "ecl",
    [
        "<<195967001",
        "<<404684003:363698007=<<39607008",
        "<<50043002:[0..0]363698007=*",
        "<<64572001:116676008=(<<49755003 MINUS <<4147007)",
    ],
)
def test_check_ecl_allows_scoped_queries(ecl: str):
    check_ecl(SNOMED_FR_ECL + ecl)


def test_zero_cardinality_on_root_is_configurable():
    ecl = SNOMED_FR_ECL + "<<404684003:[0..0]363698007=*"

    with pytest.raises(GuardError, match=r"\[E_QUERY_BLOCKED\].*\[0\.\.0\]"):
        QueryGuards().check_ecl(ecl)
    QueryGuards(block_zero_cardinality_ecl=False).check_ecl(ecl)


# ── Parcours niveau par niveau ──────────────────────────────────────────


def test_descending_traversal_suggests_descendants(clock: FakeClock):
    tracker = TraversalTracker(3, clock=clock)
    for code in ("A", "B", "C"):
        tracker.check("down", CIM10, code)

    with pytest.raises(GuardError, match=r"\[E_RECURSIVE_TRAVERSAL\].*get_descendants"):
        tracker.check("down", CIM10, "D")


def test_ascending_traversal_suggests_ancestors(clock: FakeClock):
    tracker = TraversalTracker(2, clock=clock)
    tracker.check("up", CIM10, "J45.0")
    tracker.check("up", CIM10, "J45")

    with pytest.raises(GuardError, match=r"get_parents.*get_ancestors.*code='J40-J47'"):
        tracker.check("up", CIM10, "J40-J47")


def test_traversal_counts_distinct_codes_per_direction(clock: FakeClock):
    tracker = TraversalTracker(2, clock=clock)
    tracker.check("down", CIM10, "A")
    tracker.check("down", CIM10, "B")

    tracker.check("down", CIM10, "A")  # même code : pas une nouvelle étape
    tracker.check("up", CIM10, "C")  # autre sens : compteur séparé
    with pytest.raises(GuardError):
        tracker.check("down", CIM10, "D")


def test_traversal_window_expires(clock: FakeClock):
    tracker = TraversalTracker(1, clock=clock)
    tracker.check("down", CIM10, "A")
    clock.now += 60

    tracker.check("down", CIM10, "B")


# ── Pagination et nombre ────────────────────────────────────────────────


def test_check_pagination_depth():
    check_pagination_depth(0, 1000)
    check_pagination_depth(999, 1000)

    with pytest.raises(GuardError, match=r"\[E_PAGINATION_DEPTH_EXCEEDED\].*1 000"):
        check_pagination_depth(1000, 1000)


def test_clamp_page_size():
    assert clamp_page_size(20, 100) == 20
    assert clamp_page_size(500, 100) == 100
    assert clamp_page_size(0, 100) == 1
    assert clamp_page_size(-5, 100) == 1


# ── Cache des réponses ──────────────────────────────────────────────────


def test_response_cache_ttl_and_copies(clock: FakeClock):
    cache = ResponseCache(ttl_seconds=10, max_entries=10, clock=clock)
    value = {"contains": [{"code": "J45"}]}
    cache.set("k", value)

    value["contains"].append({"code": "modifié"})
    hit = cache.get("k")
    assert hit == {"contains": [{"code": "J45"}]}
    assert hit is not None
    hit["contains"].clear()
    assert cache.get("k") == {"contains": [{"code": "J45"}]}

    clock.now += 11
    assert cache.get("k") is None


def test_response_cache_evicts_least_recently_used(clock: FakeClock):
    cache = ResponseCache(ttl_seconds=60, max_entries=2, clock=clock)
    cache.set("a", {"v": 1})
    cache.set("b", {"v": 2})
    cache.get("a")  # « a » devient le plus récent
    cache.set("c", {"v": 3})

    assert cache.get("b") is None
    assert cache.get("a") == {"v": 1}


# ── Coupe-circuit ───────────────────────────────────────────────────────


def test_breaker_opens_on_429_for_retry_after(clock: FakeClock):
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure(429, 20)

    with pytest.raises(
        BackendUnavailableError, match=r"\[E_BACKEND_UNAVAILABLE\].*~21 s"
    ):
        breaker.check()
    clock.now += 20
    breaker.check()


def test_breaker_opens_on_503_with_default_cooldown(clock: FakeClock):
    breaker = CircuitBreaker(cooldown_seconds=30, clock=clock)
    breaker.record_failure(503, None)

    clock.now += 29
    with pytest.raises(BackendUnavailableError):
        breaker.check()


def test_breaker_caps_retry_after(clock: FakeClock):
    breaker = CircuitBreaker(max_cooldown_seconds=300, clock=clock)
    breaker.record_failure(429, 3600)

    clock.now += 300
    breaker.check()


def test_breaker_opens_after_consecutive_server_failures(clock: FakeClock):
    breaker = CircuitBreaker(failure_threshold=3, clock=clock)
    breaker.record_failure(500, None)
    breaker.record_failure(None, None)  # délai dépassé ou échec réseau
    breaker.check()

    breaker.record_failure(502, None)
    with pytest.raises(BackendUnavailableError):
        breaker.check()


def test_breaker_success_resets_and_client_errors_are_ignored(clock: FakeClock):
    breaker = CircuitBreaker(failure_threshold=2, clock=clock)
    breaker.record_failure(500, None)
    breaker.record_success()
    breaker.record_failure(500, None)
    for _ in range(5):
        breaker.record_failure(404, None)  # erreur de la requête, pas du serveur

    breaker.check()


def test_breaker_allows_a_single_probe(clock: FakeClock):
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure(500, None)
    clock.now += 30

    breaker.check()  # appel test
    with pytest.raises(BackendUnavailableError, match="en cours de vérification"):
        breaker.check()


def test_breaker_backoff_doubles_until_success(clock: FakeClock):
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_seconds=30, block_after_seconds=3600, clock=clock
    )
    breaker.record_failure(500, None)  # pause de 30 s
    for pause in (60, 120, 240, 300, 300):  # doublée, plafonnée à 5 min
        clock.now = breaker._open_until  # fin de la pause
        breaker.check()  # appel test
        breaker.record_failure(500, None)  # échec : pause allongée
        with pytest.raises(BackendUnavailableError, match=rf"~{pause + 1} s"):
            breaker.check()

    clock.now = breaker._open_until
    breaker.check()
    breaker.record_success()  # réussite : coupe-circuit fermé, cycle remis à zéro
    breaker.check()
    breaker.record_failure(500, None)
    with pytest.raises(BackendUnavailableError, match=r"~31 s"):
        breaker.check()


def test_breaker_probe_client_error_counts_as_success(clock: FakeClock):
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    breaker.record_failure(500, None)
    clock.now += 30
    breaker.check()

    breaker.record_failure(404, None)  # le SMT a répondu

    breaker.check()
    breaker.check()  # fermé : plus d'appel test exclusif


def test_breaker_blocks_after_five_minutes_without_success(clock: FakeClock):
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_seconds=30, block_after_seconds=300, clock=clock
    )
    breaker.record_failure(500, None)  # début de la panne : pause de 30 s
    for _ in range(3):  # appels test à 30, 90 puis 210 s, tous en échec
        clock.now += breaker._open_until - clock.now  # fin de la pause
        breaker.check()
        breaker.record_failure(500, None)
    assert not breaker.blocked

    clock.now = 1000.0 + 300  # échéance atteinte avant la fin de la pause
    breaker.check()  # dernier appel test, sans attendre la fin de la pause
    breaker.record_failure(500, None)

    assert breaker.blocked
    with pytest.raises(BackendBlockedError, match=r"\[E_BACKEND_BLOCKED\].*redémarrer"):
        breaker.check()
    breaker.record_success()  # même un succès tardif ne débloque pas
    clock.now += 3600
    with pytest.raises(BackendBlockedError):
        breaker.check()


def test_breaker_last_probe_success_avoids_block(clock: FakeClock):
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_seconds=300, block_after_seconds=300, clock=clock
    )
    breaker.record_failure(500, None)
    clock.now += 300

    breaker.check()
    breaker.record_success()

    assert not breaker.blocked
    breaker.check()


def test_outage_starts_at_first_opening(clock: FakeClock):
    breaker = CircuitBreaker(
        failure_threshold=2, cooldown_seconds=30, block_after_seconds=300, clock=clock
    )
    breaker.record_failure(500, None)  # échec isolé, coupe-circuit fermé
    clock.now += 3600  # une heure plus tard, nouvelle panne
    breaker.record_failure(500, None)  # ouverture : début de la panne
    clock.now += 30
    breaker.check()
    breaker.record_failure(500, None)

    assert not breaker.blocked  # 30 s de panne seulement


def test_rate_limit_releases_the_probe(clock: FakeClock):
    guards = QueryGuards(
        rate_limit_calls=1,
        backend_failure_threshold=1,
        backend_cooldown_seconds=10,
        clock=clock,
    )
    guards.before_backend_call()  # consomme le budget
    guards.record_failure(500, None)
    clock.now += 10

    with pytest.raises(RateLimitError):
        guards.before_backend_call()  # appel test refusé par le débit
    clock.now += 60
    guards.before_backend_call()  # l'appel test reste disponible


# ── Débit et concurrence ────────────────────────────────────────────────


def test_rolling_window(clock: FakeClock):
    limiter = RollingRateLimiter(2, 60, clock=clock)
    limiter.check()
    clock.now += 30
    limiter.check()

    with pytest.raises(RateLimitError, match=r"\[E_RATE_LIMIT\].*~31s"):
        limiter.check()

    clock.now += 30  # le premier appel sort de la fenêtre
    limiter.check()


def test_breaker_is_checked_before_consuming_budget(clock: FakeClock):
    guards = QueryGuards(rate_limit_calls=1, clock=clock)
    guards.record_failure(503, 10)

    with pytest.raises(BackendUnavailableError):
        guards.before_backend_call()
    clock.now += 10
    guards.before_backend_call()  # le refus n'a pas entamé le budget


def test_concurrency_cap():
    limiter = ConcurrencyLimiter(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal active, peak
        with limiter:
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak == 2


# ── Avertissement sur gros résultat ─────────────────────────────────────


def test_large_result_advisory():
    result = add_large_result_warning({"total": 5000, "returned": 20}, 1000, False)
    assert "5000 concepts" in result["_large_result_warning"]
    assert "20" in result["_large_result_warning"]

    assert "_large_result_warning" not in add_large_result_warning(
        {"total": 5000}, 1000, True
    )
    assert "_large_result_warning" not in add_large_result_warning(
        {"total": 10}, 1000, False
    )
    assert "_large_result_warning" not in add_large_result_warning(
        {"total": None}, 1000, False
    )
