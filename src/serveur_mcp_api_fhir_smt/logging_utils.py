"""Journalisation sur la sortie d'erreur.

En stdio, la sortie standard est réservée au protocole MCP : rien d'autre ne doit
y être écrit.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: int) -> None:
    """Configure la journalisation racine sur stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)
