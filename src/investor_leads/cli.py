"""Command-line entry point: `investor-leads run` and `investor-leads export`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from datetime import datetime

from . import emailer
from .config import ConfigError, Keys, load_settings
from .llm import LLMError
from .pipeline import export_only, run

DEFAULT_CONFIG = "config.yaml"


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--industry", help='One or more industries, comma-separated, e.g. "gaming, entertainment"')
    parser.add_argument("--stage", help='One or more funding stages, comma-separated, e.g. "pre-seed, seed"')
    parser.add_argument("--days", type=int, help="How many days back to look (default 7)")
    parser.add_argument("--config", help=f"Path to a YAML config file (default: {DEFAULT_CONFIG} if present)")
    parser.add_argument(
        "--min-confidence",
        choices=["low", "medium", "high"],
        help="Weakest leads to include (default medium; use low to add partial fits)",
    )
    parser.add_argument("--email", action="store_true", help="Email the spreadsheet to the recipients in config.yaml")
    parser.add_argument("--email-to", help="Comma-separated recipients, instead of the ones in config.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="investor-leads",
        description="Find recent LinkedIn and X posts from investors actively funding your industry and stage.",
    )
    parser.add_argument("--version", action="version", version=f"investor-leads {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="Search, classify and export leads")
    _add_common(run_parser)
    run_parser.add_argument("--platforms", help="Comma-separated: x, linkedin (default: both)")
    run_parser.add_argument("--searches", type=int, help="Searches to run on each platform (default 12)")
    run_parser.add_argument("--posts-per-search", type=int, help="Posts to read from each search (default 25)")
    run_parser.add_argument("--dry-run", action="store_true", help="Show the search plan and cost estimate, then stop")
    run_parser.add_argument(
        "--only-new",
        action="store_true",
        help="List only leads from posts found for the first time in this run (for daily runs)",
    )
    run_parser.add_argument("--replan", action="store_true", help="Ask the model for a fresh search plan")

    export_parser = commands.add_parser("export", help="Rebuild the spreadsheet from posts already collected")
    _add_common(export_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()

    config_path = args.config or (DEFAULT_CONFIG if Path(DEFAULT_CONFIG).exists() else None)
    try:
        settings = load_settings(
            config_path,
            {
                "industry": args.industry,
                "stage": args.stage,
                "days": args.days,
                "min_confidence": args.min_confidence,
            },
        )
        # Check the mail settings before spending anything, so a typo does not waste a run.
        smtp = to = None
        if args.email or args.email_to:
            smtp = emailer.SmtpConfig.from_env()
            smtp.check()
            to = emailer.recipients(settings, args.email_to)

        if args.command == "export":
            result = export_only(settings)
            return _email(result, settings, smtp, to, only_new=False)

        if args.platforms:
            chosen = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
            unknown = chosen - {"x", "linkedin"}
            if unknown:
                raise ConfigError(f"Unknown platform: {', '.join(sorted(unknown))}. Use x and/or linkedin.")
            settings.x.enabled = "x" in chosen
            settings.linkedin.enabled = "linkedin" in chosen
        if args.searches is not None:
            settings.x.max_queries = settings.linkedin.max_keywords = args.searches
        if args.posts_per_search is not None:
            settings.x.max_posts_per_query = settings.linkedin.max_posts_per_keyword = args.posts_per_search

        result = run(settings, Keys.from_env(), dry_run=args.dry_run, replan=args.replan, only_new=args.only_new)
    except (ConfigError, LLMError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if result.warnings:
        print("\nWarnings:")
        for warning in result.warnings:
            print(f"  - {warning}")
    if args.dry_run:
        return 0
    return _email(result, settings, smtp, to, only_new=args.only_new)


def _email(result, settings, smtp, to, *, only_new: bool) -> int:
    """Send the results if email was requested. Returns the process exit code."""
    if not smtp or not to:
        return 0
    message = emailer.compose(result, settings, sender=smtp.sender, to=to, when=datetime.now(), only_new=only_new)
    try:
        emailer.send(message, smtp)
    except emailer.EmailError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"\nEmailed {len(to)} {'recipient' if len(to) == 1 else 'recipients'}: {message['Subject']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
