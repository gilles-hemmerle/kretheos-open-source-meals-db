"""Command line of kratheos-food-data.

Commands:

* `build`: obtain every enabled source and rebuild the database in `dist/`;
* `download`: only obtain (or verify, or restore from a mirror) the sources;
* `sources`: list the sources, their licence and whether they are enabled;
* `release-files`: lay out in a directory what a release must carry, large
  files split into parts;
* `kratheos-init`: create an empty `kratheos.sqlite` when there is none;
* `ids-diff`: compare the identifier lists of two builds;
* `lookup`: find foods in the built database by barcode, identifier or name;
* `fastfood`: measure how prepared dishes and restaurant chains are covered.

Run `food-data <command> --help` for the options of a command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, fastfood, kratheos, lookup, mirror, sqlite_export
from .build import (
    DEFAULT_MIN_FREE_GB,
    FOOD_DATABASE_NAME,
    BuildOptions,
    DiskSpaceError,
    Workspace,
    run_build,
    run_download,
)
from .config import ConfigError, load_sources
from .download import DownloadError

LISTED_IDENTIFIERS = 50


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


def _add_input_options(command: argparse.ArgumentParser) -> None:
    """Add the options saying where source files are taken from.

    Args:
        command: The sub-command parser to complete.
    """
    command.add_argument("--only", help="comma-separated source ids to restrict to")
    command.add_argument(
        "--from-mirror",
        action="store_true",
        help="use the stored copies (sources/ and mirror/) and never contact a publisher",
    )
    command.add_argument(
        "--mirror-url",
        help="base address of a remote mirror (a release) for copies missing from mirror/",
    )
    command.add_argument(
        "--override",
        action="append",
        metavar="SOURCE/FILE=PATH",
        help="use PATH instead of one source file, for this run only (repeatable)",
    )
    command.add_argument(
        "--force-download", action="store_true", help="download every file again from upstream"
    )
    command.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_GB,
        help=f"stop rather than leave less free disk space (default {DEFAULT_MIN_FREE_GB:g})",
    )


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser with every command and option.
    """
    parser = argparse.ArgumentParser(
        prog="food-data",
        description="Rebuilds one aggregated food database from open sources.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="directory of the repository (default: current directory)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="obtain the sources and rebuild the database")
    _add_input_options(build)
    build.add_argument(
        "--all-formats",
        action="store_true",
        help="also write the product tables as CSV and JSON Lines (large)",
    )

    download = commands.add_parser("download", help="obtain, verify or restore the source files")
    _add_input_options(download)

    commands.add_parser("sources", help="list the sources and their licences")

    release = commands.add_parser(
        "release-files", help="prepare the files of mirror/ to attach to a release"
    )
    release.add_argument("--out", type=Path, help="destination directory (default: release/)")

    commands.add_parser("kratheos-init", help="create an empty kratheos.sqlite if missing")

    diff = commands.add_parser("ids-diff", help="compare the identifier lists of two builds")
    diff.add_argument("old", type=Path, help="identifiers.txt.gz of the earlier build")
    diff.add_argument("new", type=Path, help="identifiers.txt.gz of the later build")
    diff.add_argument("--json", action="store_true", help="print every identifier as JSON")

    find = commands.add_parser("lookup", help="find foods by barcode, identifier or name")
    find.add_argument("query", help="a barcode, an identifier, or the beginning of words")
    find.add_argument("--lang", help="language of the name to show, e.g. fr")
    find.add_argument("--limit", type=int, default=10, help="maximum number of foods")
    find.add_argument("--json", action="store_true", help="print JSON instead of text")

    chains = commands.add_parser("fastfood", help="measure dishes and restaurant chains")
    chains.add_argument("--json", action="store_true", help="print JSON instead of text")
    chains.add_argument("--examples", type=int, default=4, help="examples per group")
    return parser


def _options(args: argparse.Namespace) -> BuildOptions:
    """Turn the command-line arguments into build options.

    Args:
        args: The parsed arguments of `build` or `download`.

    Returns:
        The options.
    """
    return BuildOptions(
        only=_split(args.only),
        from_mirror=args.from_mirror,
        mirror_url=args.mirror_url,
        overrides=mirror.parse_overrides(args.override),
        force_download=args.force_download,
        all_formats=getattr(args, "all_formats", False),
        min_free_gb=args.min_free_gb,
    )


def _print_sources(root: Path) -> None:
    """Print the source list.

    Args:
        root: Directory holding `sources.toml`.
    """
    for source in load_sources(root / "sources.toml"):
        state = "enabled" if source.enabled else "disabled"
        print(f"{source.id:<9} {state:<9} {source.licence:<18} {source.name}")
        stored = {"git": "sources/", "release": "mirror/"}.get(source.mirror, "-")
        print(f"{'':<9} version: {source.version or '-'}; stored in: {stored}")
        if not source.enabled:
            print(f"{'':<9} reason: {source.reason}")


def _print_identifier_diff(result: dict, as_json: bool) -> None:
    """Print the comparison of two identifier lists.

    Args:
        result: The comparison made by `sqlite_export.compare_identifiers`.
        as_json: Print everything as JSON instead of a summary.
    """
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"old: {result['old']:,} identifiers; new: {result['new']:,}")
    for label in ("added", "removed"):
        identifiers = result[label]
        print(f"{label}: {len(identifiers):,}")
        for identifier in identifiers[:LISTED_IDENTIFIERS]:
            print(f"  {identifier}")
        if len(identifiers) > LISTED_IDENTIFIERS:
            print(f"  ... and {len(identifiers) - LISTED_IDENTIFIERS:,} more (--json lists all)")


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
            run_build(workspace, _options(args), log=_log)
        elif args.command == "download":
            run_download(workspace, _options(args), log=_log)
        elif args.command == "sources":
            _print_sources(root)
        elif args.command == "release-files":
            out_dir = (args.out or root / "release").resolve()
            attached = mirror.prepare_release(workspace.mirror, out_dir, log=_log)
            for entry in attached:
                print(f"{entry['size']:>14,}  {entry['sha256']}  {entry['asset']}")
            total = sum(entry["size"] for entry in attached)
            print(f"{total:>14,}  bytes in {len(attached)} files under {out_dir}")
        elif args.command == "kratheos-init":
            if workspace.kratheos.exists():
                print(f"{workspace.kratheos} already exists; left as it is")
            else:
                kratheos.create_database(workspace.kratheos)
                print(f"created {workspace.kratheos}")
        elif args.command == "ids-diff":
            result = sqlite_export.compare_identifiers(args.old, args.new)
            _print_identifier_diff(result, args.json)
        elif args.command == "lookup":
            database = workspace.dist / FOOD_DATABASE_NAME
            records = lookup.find(database, args.query, language=args.lang, limit=args.limit)
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
    except (
        ConfigError,
        DownloadError,
        DiskSpaceError,
        FileNotFoundError,
        kratheos.LayerError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
