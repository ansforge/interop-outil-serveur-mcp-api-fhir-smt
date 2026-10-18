"""Client HTTP synchrone vers l'API FHIR du SMT, avec erreurs normalisées.

Les outils MCP synchrones sont exécutés dans un thread par le SDK : un client
httpx synchrone partagé (thread-safe) suffit et réutilise ses connexions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from . import config

# Ontoserver préfixe ses diagnostics par un identifiant de requête : "[uuid]: message"
_REQUEST_ID_PREFIX_RE = re.compile(r"^\[[0-9a-fA-F-]{8,}\]:\s*")


class HttpRequestError(RuntimeError):
    """Échec d'une requête HTTP, avec un message exploitable par le modèle."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        outcome: dict[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.outcome = outcome
        # Délai d'attente demandé par le serveur (en-tête Retry-After), en secondes
        self.retry_after = retry_after


def outcome_message(outcome: dict[str, Any]) -> str | None:
    """Message principal d'une ressource OperationOutcome, sans identifiant de
    requête.
    """
    for issue in outcome.get("issue", []):
        if not isinstance(issue, dict) or issue.get("severity") not in (
            "error",
            "fatal",
        ):
            continue
        details = issue.get("details")
        text = details.get("text") if isinstance(details, dict) else None
        message = text or issue.get("diagnostics")
        if isinstance(message, str) and message:
            return _REQUEST_ID_PREFIX_RE.sub("", message)
    return None


class HttpClient:
    def __init__(
        self,
        base_url: str = config.FHIR_BASE_URL,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # `base_url` et `transport` ne servent qu'aux tests
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url + "/",
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={
                "Accept": "application/fhir+json",
                "User-Agent": config.USER_AGENT,
            },
            transport=transport,
        )

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _is_own_url(self, url: str) -> bool:
        """Vrai si `url` est l'URL de base ou se trouve sous elle (`/…` ou `?…`)."""
        base = self.base_url
        return url.startswith(base) and url[len(base) : len(base) + 1] in ("", "/", "?")

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if path.startswith(("http://", "https://")) and not self._is_own_url(path):
            # Liens de pagination : on ne suit que les URL du SMT
            raise HttpRequestError(f"URL hors du serveur refusée : {path}")
        label = f"{method} {_path_of(path)}"
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise HttpRequestError(f"Délai dépassé pour {label}") from exc
        except httpx.RequestError as exc:
            raise HttpRequestError(f"Échec de la requête {label} : {exc}") from exc

        data: Any = None
        try:
            data = response.json()
        except ValueError:
            pass

        if response.status_code >= 400:
            outcome = (
                data
                if isinstance(data, dict)
                and data.get("resourceType") == "OperationOutcome"
                else None
            )
            detail = (outcome_message(outcome) if outcome else None) or response.text[
                :200
            ]
            raise HttpRequestError(
                f"HTTP {response.status_code} pour {label} : {detail}",
                status_code=response.status_code,
                outcome=outcome,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if not isinstance(data, dict):
            raise HttpRequestError(f"Réponse JSON invalide pour {label}")
        return data

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET sur un chemin relatif à l'URL de base, ou sur une URL absolue du même
        serveur.
        """
        return self._request("GET", path, params=_clean_params(params))

    def post(
        self, path: str, body: dict[str, Any], params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            path,
            params=_clean_params(params),
            json=body,
            headers={"Content-Type": "application/fhir+json"},
        )


def parse_retry_after(value: str | None) -> float | None:
    """En-tête Retry-After en secondes : nombre de secondes ou date HTTP."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _path_of(path: str) -> str:
    return urlparse(path).path or path
