"""Unified collector CLI: one entry point for every harvesting path.

WHY THIS EXISTS
---------------
The collectors grew one runner script at a time and the repository ended up
with two dozen ``_run_*.py`` files at its root, each with its own argument
spelling, its own output format and its own idea of where the report goes.
None of them were discoverable (``python -m paper_ingest --help`` did not
mention a single one of them), and a run could not be reproduced from the
command line alone.

This module is the single entry point. It covers the paths that exist today:

============================  ==================================================
``venues``                   the venue registry
``status``                   the recent sync jobs
``sync`` / ``sync-years``    the official proceedings harvester
``sync-api`` / ``sync-api-years``  the multi-source API harvester
``journals``                 the OpenAlex journal collector
``journal-abstracts``        the Europe PMC / publisher abstract backfill
``jmlr``                     the jmlr.org journal harvester
``coverage``                 the expected-size audit of the whole corpus
``harvest``                  a plan that runs several of the above in order
============================  ==================================================

Every command prints one JSON object (or one JSON array) on stdout, so a run
can be piped into ``jq`` or captured by a script without parsing prose. The
exit code is non-zero when a command failed, which is what a scheduler needs.

The commands are thin: the work lives in the modules
(:mod:`paper_ingest.service`, :mod:`paper_ingest.sync_api`,
:mod:`paper_ingest.provider_journals`, :mod:`paper_ingest.journal_abstracts`,
:mod:`paper_ingest.jmlr`, :mod:`paper_ingest.manifest`). This file only
translates arguments and prints results, so the behaviour of a command is the
behaviour of its module -- and is testable there.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

from . import db
from .config import venues as venue_registry

__all__ = ["main", "build_parser", "run_command"]


def _emit(payload: Any) -> None:
    """Print one JSON document on stdout.

    ``ensure_ascii=False`` keeps the venue names readable; the terminal gets
    UTF-8, and the JSON is still valid for a machine reader.
    """
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    sys.stdout.write("\n")
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    """The full command surface."""
    parser = argparse.ArgumentParser(
        prog="paper_ingest",
        description="Harvest paper metadata from official sources.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("venues", help="print the venue registry")
    sub.add_parser("status", help="print the recent sync jobs")

    # -- official proceedings ------------------------------------------------
    sync = sub.add_parser("sync", help="harvest one venue-year from the publisher's site")
    sync.add_argument("--venue", required=True)
    sync.add_argument("--year", type=int, default=2027)
    sync.add_argument("--full", action="store_true")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--limit", type=int, default=None)

    span = sub.add_parser("sync-years", help="harvest several years of one venue")
    span.add_argument("--venue", required=True)
    span.add_argument("--from-year", type=int, default=2022)
    span.add_argument("--to-year", type=int, default=2026)
    span.add_argument("--limit", type=int, default=None)

    # -- multi-source API ----------------------------------------------------
    api = sub.add_parser("sync-api", help="harvest via official APIs (resilient multi-source)")
    api.add_argument("--venue", required=True)
    api.add_argument("--year", type=int, default=2027)
    api.add_argument("--limit", type=int, default=None)
    api.add_argument("--target", type=int, default=None,
                     help="stop after this many unique papers")
    api.add_argument("--dry-run", action="store_true")
    api.add_argument("--mailto", default=None, help="contact email for the polite API pools")
    api.add_argument("--api-key", default=None, help="OpenAlex API key (or set OPENALEX_API_KEY)")
    api.add_argument("--arxiv-delay", type=float, default=3.0,
                     help="seconds between arXiv requests")

    api_span = sub.add_parser("sync-api-years", help="harvest several years via official APIs")
    api_span.add_argument("--venue", required=True)
    api_span.add_argument("--from-year", type=int, default=2022)
    api_span.add_argument("--to-year", type=int, default=2026)
    api_span.add_argument("--dry-run", action="store_true")
    api_span.add_argument("--mailto", default=None)
    api_span.add_argument("--api-key", default=None)
    api_span.add_argument("--limit", type=int, default=None)

    # -- journals ------------------------------------------------------------
    journals = sub.add_parser("journals", help="harvest journal papers from OpenAlex")
    journals.add_argument("--venue", default="all",
                          help="journal key (see `venues`), or 'all'")
    journals.add_argument("--year", type=int, default=None,
                          help="a single year, or every year when omitted")
    journals.add_argument("--limit", type=int, default=None)
    journals.add_argument("--no-backfill", action="store_true",
                          help="skip the abstract backfill pass")
    journals.add_argument("--since", default=None,
                          help="only fetch works OpenAlex indexed after this date (YYYY-MM-DD)")
    journals.add_argument("--incremental", action="store_true",
                          help="resume from this venue-year's stored watermark")
    journals.add_argument("--mailto", default=None)
    journals.add_argument("--api-key", default=None)

    backfill = sub.add_parser("journal-abstracts",
                              help="fill missing journal abstracts (Europe PMC, publisher pages)")
    backfill.add_argument("--venue", default="all")
    backfill.add_argument("--year", type=int, default=None)
    backfill.add_argument("--limit", type=int, default=None)
    backfill.add_argument("--workers", type=int, default=2,
                          help="publisher-page fan-out (keep it low: the publisher rate-limits)")
    backfill.add_argument("--epmc-workers", type=int, default=8,
                          help="Europe PMC fan-out")

    jmlr = sub.add_parser("jmlr", help="harvest JMLR from jmlr.org")
    jmlr.add_argument("--year", type=int, action="append",
                      help="year(s) to import; repeatable, defaults to every supported year")
    jmlr.add_argument("--limit", type=int, default=None)
    jmlr.add_argument("--force", action="store_true", help="refetch the cached volume pages")

    # -- corpus-wide ---------------------------------------------------------
    coverage = sub.add_parser("coverage",
                               help="check every venue-year against its expected size")
    coverage.add_argument("--venue", default="all")

    graph = sub.add_parser("graph", help="print the map payload for a venue")
    graph.add_argument("--venue")
    graph.add_argument("--year", type=int, default=2027)

    build = sub.add_parser("build-graph", help="build the semantic layout for a venue-year")
    build.add_argument("--venue", required=True)
    build.add_argument("--year", type=int, default=2027)

    enrich = sub.add_parser("enrich-abstracts", help="enrich missing abstracts for a venue")
    enrich.add_argument("--venue", required=True)
    enrich.add_argument("--year", type=int, default=2027)
    enrich.add_argument("--limit", type=int, default=100)

    # -- the plan ------------------------------------------------------------
    plan = sub.add_parser(
        "harvest",
        help="run a harvesting plan: journals, then the abstract backfill, then the audit",
    )
    plan.add_argument("--venue", default="all", help="journal key, or 'all'")
    plan.add_argument("--from-year", type=int, default=2022)
    plan.add_argument("--to-year", type=int, default=2026)
    plan.add_argument("--limit", type=int, default=None)
    plan.add_argument("--no-backfill", action="store_true")
    plan.add_argument("--since", default=None,
                      help="only fetch works indexed after this date (YYYY-MM-DD)")
    plan.add_argument("--incremental", action="store_true",
                      help="resume from the stored watermark of each venue-year")
    plan.add_argument("--skip-journals", action="store_true",
                      help="only backfill abstracts (the corpus is already there)")
    plan.add_argument("--workers", type=int, default=2)
    plan.add_argument("--epmc-workers", type=int, default=8)
    plan.add_argument("--mailto", default=None)
    plan.add_argument("--api-key", default=None)

    return parser


# --------------------------------------------------------------------------- #
# Command implementations                                                       #
# --------------------------------------------------------------------------- #
def _cmd_venues(args: argparse.Namespace) -> dict[str, Any]:
    return {"venues": venue_registry()}


def _cmd_status() -> list[dict[str, Any]]:
    with db.connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM sync_jobs ORDER BY id DESC LIMIT 30")]


def _cmd_sync(args: argparse.Namespace) -> dict[str, Any]:
    import asyncio

    from .service import sync_many

    keys = list(venue_registry()) if args.venue == "all" else [args.venue]
    results = asyncio.run(sync_many(keys, args.year, full=args.full,
                                    dry_run=args.dry_run, limit=args.limit))
    return {"results": results}


def _cmd_sync_years(args: argparse.Namespace) -> dict[str, Any]:
    import asyncio

    from .service import sync_many

    if args.from_year > args.to_year:
        raise ValueError("--from-year must be <= --to-year")
    keys = list(venue_registry()) if args.venue == "all" else [args.venue]
    results: list[Any] = []
    for year in range(args.from_year, args.to_year + 1):
        results.extend(asyncio.run(sync_many(keys, year, limit=args.limit)))
    return {"results": results}


def _cmd_sync_api(args: argparse.Namespace) -> dict[str, Any]:
    from .sync_api import sync_api

    return sync_api(args.venue, args.year, limit=args.limit, target=args.target,
                    dry_run=args.dry_run, mailto=args.mailto, api_key=args.api_key,
                    arxiv_delay=args.arxiv_delay)


def _cmd_sync_api_years(args: argparse.Namespace) -> dict[str, Any]:
    from .sync_api import sync_api

    if args.from_year > args.to_year:
        raise ValueError("--from-year must be <= --to-year")
    results = []
    for year in range(args.from_year, args.to_year + 1):
        results.append(sync_api(args.venue, year, limit=args.limit,
                                 dry_run=args.dry_run, mailto=args.mailto,
                                 api_key=args.api_key))
    return {"results": results}


def _journal_keys(venue: str) -> list[str]:
    from .provider_journals import JOURNAL_SOURCES

    if venue == "all":
        return sorted(JOURNAL_SOURCES)
    if venue not in JOURNAL_SOURCES:
        raise ValueError(
            f"unknown journal {venue!r}; known: {', '.join(sorted(JOURNAL_SOURCES))}")
    return [venue]


def _cmd_journals(args: argparse.Namespace) -> dict[str, Any]:
    from .provider_journals import JOURNAL_YEARS

    years = [args.year] if args.year else list(JOURNAL_YEARS)
    results = []
    for key in _journal_keys(args.venue):
        for year in years:
            result = _harvest_one_journal(key, year, args)
            results.append(result)
    return {"results": results}


def _harvest_one_journal(key: str, year: int, args: argparse.Namespace) -> dict[str, Any]:
    """Harvest one journal-year and write it to the corpus.

    The write path is the same one the ad-hoc runner used: the OpenAlex
    provider for the records, the shared abstract backfill for the missing
    ones, and a sync_jobs row for the outcome.
    """
    from .provider_journals import JOURNAL_SOURCES

    from .journals_sync import import_journal_year

    source_id, journal_name = JOURNAL_SOURCES[key]
    started = time.time()
    result = import_journal_year(
        key, year,
        limit=getattr(args, "limit", None),
        backfill=not getattr(args, "no_backfill", False),
        mailto=getattr(args, "mailto", None),
        api_key=getattr(args, "api_key", None),
        since=getattr(args, "since", None),
        incremental=bool(getattr(args, "incremental", False)),
    )
    result["seconds"] = round(time.time() - started, 1)
    result["source_id"] = source_id
    result["journal_name"] = journal_name
    return result


def _cmd_journal_abstracts(args: argparse.Namespace) -> dict[str, Any]:
    from .journal_abstracts import backfill_journal_abstracts
    from .provider_journals import JOURNAL_YEARS

    years = [args.year] if args.year else list(JOURNAL_YEARS)
    results = []
    for key in _journal_keys(args.venue):
        for year in years:
            results.append(backfill_journal_abstracts(
                key, year, limit=args.limit,
                workers=args.workers, epmc_workers=args.epmc_workers))
    return {"results": results}


def _cmd_jmlr(args: argparse.Namespace) -> dict[str, Any]:
    from .jmlr import YEARS, sync

    years = args.year or list(YEARS)
    return {"results": [sync(year, force=args.force, limit=args.limit)
                        for year in years]}


def _cmd_coverage(args: argparse.Namespace) -> dict[str, Any]:
    from .manifest import load_manifest, verify_corpus

    manifest = load_manifest()
    verdicts = verify_corpus(manifest)
    # The last element is the summary, not a verdict.
    summary = verdicts[-1] if verdicts else {
        "checked": 0, "ok": 0, "short": 0, "over": 0, "unknown": 0}
    shortfalls = [v for v in verdicts[:-1]
                  if isinstance(v, dict) and v.get("status") in ("short", "over")]
    return {"summary": summary, "shortfalls": shortfalls,
            "expectations": len(manifest)}


def _cmd_graph(args: argparse.Namespace) -> dict[str, Any]:
    from .service import graph

    return graph(args.venue, args.year)


def _cmd_build_graph(args: argparse.Namespace) -> dict[str, Any]:
    from .service import generate_graph

    return generate_graph(args.venue, args.year)


def _cmd_enrich_abstracts(args: argparse.Namespace) -> dict[str, Any]:
    from .official import enrich_missing_abstracts

    return enrich_missing_abstracts(args.venue, args.year, args.limit)


def _cmd_harvest(args: argparse.Namespace) -> dict[str, Any]:
    """The plan: journals, then the abstract backfill, then the audit.

    A plan exists because the three steps have a real order and a real
    dependency: the harvest brings the records, the backfill brings their
    abstracts, and the audit says whether the result is complete. Running
    them by hand in the wrong order is how a corpus ends up with titles but
    no abstracts.
    """
    steps: list[dict[str, Any]] = []
    if not args.skip_journals:
        for key in _journal_keys(args.venue):
            for year in range(args.from_year, args.to_year + 1):
                steps.append({"step": "journals", "venue": key, "year": year,
                              **_harvest_one_journal(key, year, args)})
    else:
        for key in _journal_keys(args.venue):
            for year in range(args.from_year, args.to_year + 1):
                from .journal_abstracts import backfill_journal_abstracts

                steps.append({"step": "journal-abstracts", "venue": key, "year": year,
                              **backfill_journal_abstracts(
                                  key, year, limit=args.limit,
                                  workers=args.workers, epmc_workers=args.epmc_workers)})
    coverage = _cmd_coverage(args)
    return {"steps": steps, "coverage": coverage}


#: Command name -> implementation.
COMMANDS: dict[str, Callable[[argparse.Namespace], Any]] = {
    "venues": _cmd_venues,
    "status": _cmd_status,
    "sync": _cmd_sync,
    "sync-years": _cmd_sync_years,
    "sync-api": _cmd_sync_api,
    "sync-api-years": _cmd_sync_api_years,
    "journals": _cmd_journals,
    "journal-abstracts": _cmd_journal_abstracts,
    "jmlr": _cmd_jmlr,
    "coverage": _cmd_coverage,
    "graph": _cmd_graph,
    "build-graph": _cmd_build_graph,
    "enrich-abstracts": _cmd_enrich_abstracts,
    "harvest": _cmd_harvest,
}


def run_command(args: argparse.Namespace) -> Any:
    """Dispatch one parsed command and return its payload."""
    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        raise ValueError(f"unknown command {args.command!r}")
    return handler(args)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        db.init_db()
        payload = run_command(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not crash
        _emit({"ok": False, "command": args.command,
               "error": f"{type(exc).__name__}: {exc}"})
        return 1
    _emit({"ok": True, "command": args.command, "result": payload})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
