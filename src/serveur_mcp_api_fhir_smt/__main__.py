"""Point d'entrée : `serveur-mcp-api-fhir-smt [--log-level NIVEAU]`.

Le serveur fonctionne en stdio : il est lancé par le client MCP (Claude Desktop,
Claude Code, VS Code…), qui dialogue avec lui sur l'entrée et la sortie standard.
Il interroge toujours l'API FHIR du SMT (voir config.py).
"""

from __future__ import annotations

import argparse
import logging

from .logging_utils import configure_logging
from .mcp_app import create_mcp_app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serveur-mcp-api-fhir-smt",
        description="Serveur MCP d'accès aux API FHIR du Serveur Multi-Terminologies",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Niveau de journalisation sur la sortie d'erreur (défaut : INFO)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    configure_logging(getattr(logging, args.log_level))
    create_mcp_app().run(transport="stdio")


if __name__ == "__main__":
    main()
