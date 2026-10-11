"""Command line of kratheos-food-data.

Commands:

* `build`: download every enabled source and rebuild the database in `dist/`;
* `download`: only download (or verify) the source files;
* `sources`: list the sources, their licence and whether they are enabled;
* `lookup`: print the records of a barcode or the foods matching a name,
  from a built database;
* `fastfood`: measure how fast-food and restaurant chains are covered.

Run `food-data <command> --help` for the options of a command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, fastfood, lookup
from .build import DEFAULT_MIN_FREE_GB, DiskSpaceError, Workspace, run_build, run_download
from .config import ConfigError, load_sources
from .download import DownloadError


def _log(message: str) -> None:
    """Print a progress line immediately.

    Args:
        message: The line to print.
    """
    print(message, flush=True)


def _split(value: str | None) -> list[str] | None:
    """Split a comma-separated option.

    Args:
        value: The option value, or None when the option was not given.

    Returns:
        The non-empty items, or None.
    """
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser with every command and option.
    """
    parser = argparse.ArgumentParser(
        prog="food-data",
        description="Downloads open food databases and rebuilds one aggregated database.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="directory holding sources.toml, downloads/, work/ and dist/ (default: current)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add_download_options(command: argparse.ArgumentParser) -> None:
        """Add the options shared by `build` and `download`."""
        command.add_argument("--only", help="comma-separated source ids to restrict to")
        command.add_argument(
            "--offline", action="store_true", help="use the files already downloaded"
        )
        command.add_argument(
            "--force-download", action="store_true", help="download every file again"
        )
        command.add_argument(
            "--min-free-gb",
            type=float,
            default=DEFAULT_MIN_FREE_GB,
            help=f"stop rather than leave less free disk space (default {DEFAULT_MIN_FREE_GB:g})",
        )

    build = commands.add_parser("build", help="download the sources and rebuild the database")
    add_download_options(build)
    build.add_argument(
        "--all-formats",
        action="store_true",
        help="also write the product tables as CSV and JSON Lines (large)",
    )

    download = commands.add_parser("download", help="download or verify the source files")
    add_download_options(download)

    commands.add_parser("sources", help="list the sources and their licences")

    find = commands.add_parser("lookup", help="look a barcode or a name up in dist/")
    find.add_argument("query", help="a barcode, or words of a food name")
    find.add_argument("--limit", type=int, default=10, help="maximum number of records")
    find.add_argument("--json", action="store_true", help="print JSON instead of text")

    chains = commands.add_parser("fastfood", help="measure fast-food coverage in dist/")
    chains.add_argument("--json", action="store_true", help="print JSON instead of text")
    chains.add_argument("--examples", type=int, default=4, help="examples per chain and source")
    return parser


def _print_sources(root: Path) -> None:
    """Print the source list.

    Args:
        root: Directory holding `sources.toml`.
    """
    for source in load_sources(root / "sources.toml"):
        state = "enabled" if source.enabled else "disabled"
        print(f"{source.id:<9} {state:<9} {source.licence:<18} {source.name}")
        print(f"{'':<9} version: {source.version or '-'}; kinds: {', '.join(source.kinds)}")
        if not source.enabled:
            print(f"{'':<9} reason: {source.reason}")


def main(argv: list[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments, defaulting to those of the process.

    Returns:
        The process exit status.
    """
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    workspace = Workspace.at(root)
    try:
        if args.command == "build":
            run_build(
                workspace,
                only=_split(args.only),
                offline=args.offline,
                force_download=args.force_download,
                all_formats=args.all_formats,
                min_free_gb=args.min_free_gb,
                log=_log,
            )
        elif args.command == "download":
            run_download(
                workspace,
                only=_split(args.only),
                offline=args.offline,
                force=args.force_download,
                min_free_gb=args.min_free_gb,
                log=_log,
            )
        elif args.command == "sources":
            _print_sources(root)
        elif args.command == "lookup":
            records = lookup.find(workspace.dist, args.query, limit=args.limit)
            if args.json:
                print(json.dumps(records, indent=2, ensure_ascii=False, default=str))
            else:
                print(lookup.format_records(records))
        elif args.command == "fastfood":
            report = fastfood.measure(workspace.dist, examples=args.examples)
            if args.json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                print(fastfood.format_report(report))
    except (ConfigError, DownloadError, DiskSpaceError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
