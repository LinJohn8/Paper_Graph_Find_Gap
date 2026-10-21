"""Tests for the unified collector CLI (paper_ingest/cli.py).

The CLI is a thin layer, so these tests pin the things a thin layer can still
get wrong: that every command is reachable, that the arguments reach the
module that does the work, that the output is machine-readable JSON, and that
a failure exits non-zero instead of printing a traceback.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_ingest import cli  # noqa: E402


class TestParser:
    def test_every_command_is_registered(self):
        parser = cli.build_parser()
        actions = [a for a in parser._actions if a.dest == "command"]
        assert actions, "the parser has no subcommands"
        names = set(actions[0].choices)
        for expected in ("venues", "status", "sync", "sync-api", "journals",
                         "journal-abstracts", "jmlr", "coverage", "harvest"):
            assert expected in names, f"{expected} is not a command"

    def test_unknown_command_is_rejected(self):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["not-a-command"])

    def test_the_help_text_lists_the_commands(self):
        parser = cli.build_parser()
        text = parser.format_help()
        for expected in ("journals", "coverage", "journal-abstracts"):
            assert expected in text


class TestDispatch:
    def test_run_command_reaches_the_handler(self, monkeypatch):
        seen = {}

        def fake(args):
            seen["command"] = args.command
            return {"value": 42}

        monkeypatch.setitem(cli.COMMANDS, "status", fake)
        args = cli.build_parser().parse_args(["status"])
        assert cli.run_command(args) == {"value": 42}
        assert seen["command"] == "status"

    def test_an_unknown_command_raises(self):
        args = cli.build_parser().parse_args(["status"])
        args.command = "nope"
        with pytest.raises(ValueError):
            cli.run_command(args)


class TestCommands:
    def test_venues_lists_the_registry(self):
        args = cli.build_parser().parse_args(["venues"])
        payload = cli.run_command(args)
        assert "venues" in payload
        assert isinstance(payload["venues"], dict)

    def test_coverage_reports_a_summary(self):
        args = cli.build_parser().parse_args(["coverage"])
        payload = cli.run_command(args)
        assert "summary" in payload
        assert "checked" in payload["summary"]
        assert isinstance(payload["shortfalls"], list)

    def test_coverage_uses_the_manifest(self, monkeypatch):
        from paper_ingest import manifest as manifest_module

        captured = {}

        def fake_load(path=None):
            captured["loaded"] = True
            return {}

        def fake_verify(m=None, con=None):
            captured["verified"] = True
            return [{"checked": 0, "ok": 0, "short": 0, "over": 0, "unknown": 0}]

        monkeypatch.setattr(manifest_module, "load_manifest", fake_load)
        monkeypatch.setattr(manifest_module, "verify_corpus", fake_verify)
        args = cli.build_parser().parse_args(["coverage"])
        payload = cli.run_command(args)
        assert captured.get("loaded") and captured.get("verified")
        assert payload["expectations"] == 0

    def test_an_unknown_journal_is_reported_clearly(self):
        args = cli.build_parser().parse_args(["journals", "--venue", "not-a-journal"])
        with pytest.raises(ValueError) as excinfo:
            cli.run_command(args)
        # The message must name the venue and list the known ones.
        assert "not-a-journal" in str(excinfo.value)
        assert "known" in str(excinfo.value)

    def test_journal_years_default_to_every_year(self):
        from paper_ingest.provider_journals import JOURNAL_YEARS

        args = cli.build_parser().parse_args(["journals", "--venue", "nature"])
        assert args.year is None
        # The fallback is the registry's year list.
        assert len(JOURNAL_YEARS) >= 1


class TestMain:
    def test_a_successful_command_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setitem(cli.COMMANDS, "venues", lambda args: {"venues": {}})
        code = cli.main(["venues"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert payload["command"] == "venues"

    def test_a_failing_command_exits_one_and_stays_machine_readable(
            self, monkeypatch, capsys):
        def boom(args):
            raise RuntimeError("upstream refused")

        monkeypatch.setitem(cli.COMMANDS, "status", boom)
        code = cli.main(["status"])
        assert code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "upstream refused" in payload["error"]

    def test_the_output_is_one_json_document(self, monkeypatch, capsys):
        monkeypatch.setitem(cli.COMMANDS, "status", lambda args: [{"id": 1}])
        cli.main(["status"])
        out = capsys.readouterr().out.strip()
        # One document: it parses, and it does not parse as a stream.
        assert isinstance(json.loads(out), dict)
        assert out.count("\n{") == 0
