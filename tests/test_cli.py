"""Structural tests that don't hit the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from autonomous_dev.agent import DEFAULT_ALLOWED_TOOLS, RunConfig
from autonomous_dev.cli import build_parser, main


def test_parser_requires_repo() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["do a thing"])  # missing --repo


def test_parser_parses_args() -> None:
    parser = build_parser()
    args = parser.parse_args(["fix the bug", "--repo", "/tmp/x", "--mode", "acceptEdits"])
    assert args.prompt == "fix the bug"
    assert args.repo == Path("/tmp/x")
    assert args.mode == "acceptEdits"


def test_run_config_to_options() -> None:
    cfg = RunConfig(repo_path=Path("/tmp/x"))
    opts = cfg.to_options()
    assert opts.cwd == "/tmp/x"
    assert cfg.permission_mode == "plan"  # safe v1 default
    assert list(cfg.allowed_tools) == list(DEFAULT_ALLOWED_TOOLS)


def test_main_rejects_missing_repo(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["do a thing", "--repo", "/no/such/dir/really"])
    assert rc == 2
    assert "not a directory" in capsys.readouterr().out
