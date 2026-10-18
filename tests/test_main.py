from __future__ import annotations

import pytest

from serveur_mcp_api_fhir_smt.__main__ import build_arg_parser


def test_arg_parser_defaults():
    assert build_arg_parser().parse_args([]).log_level == "INFO"


def test_arg_parser_log_level():
    assert build_arg_parser().parse_args(["--log-level", "debug"]).log_level == "DEBUG"


@pytest.mark.parametrize("argv", [["--log-level", "verbeux"], ["--config", "c.yaml"]])
def test_arg_parser_rejects_unknown_options(argv):
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(argv)
