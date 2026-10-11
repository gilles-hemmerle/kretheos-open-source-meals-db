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

The service (see `docs/api.md` and the README):

* `serve`: answer the API over HTTP, from `food.sqlite` and the live database;
* `export-layer`: write the public `kratheos.sqlite` from the live database,
  and commit it when asked;
* `import-layer`: take over into the live database what a `kratheos.sqlite`
  holds and it lacks;
* `rebuild`: export, rebuild into a new file, compare with the database being
  served, and swap it in when it holds up;
* `database-files`: lay out what to publish so that others can install the
  database without rebuilding it;
* `install-database`: download such a database, verify it and swap it in.

Run `food-data <command> --help` for the options of a command.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from . import __version__, fastfood, kratheos, lookup, mirror, sqlite_export
from .aggregate import DEFAULT_USER_VERSION_VOTES
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
from .service import export, httpd, maintenance
from .service.live import LiveStore
from .service.settings import DEFAULT_HOST, DEFAULT_PORT, Settings, SettingsError, load_settings

EXIT_REFUSED = 2

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
    command.add_argument(
        "--user-version-votes",
        type=int,
        help="votes a version coming from a user needs to be served instead of a record of a"
        f" source (default: FOOD_DATA_USER_VERSION_VOTES, else {DEFAULT_USER_VERSION_VOTES})",
    )


def _add_service_options(command: argparse.ArgumentParser, live: bool = True) -> None:
    """Add the options saying where the files of the service are.

    Args:
        command: The sub-command parser to complete.
        live: Also add the option naming the live database.
    """
    command.add_argument(
        "--database",
        type=Path,
        help="food.sqlite being served (default: FOOD_DATA_DATABASE, else food.sqlite in"
        " FOOD_DATA_DIR, else data/food.sqlite)",
    )
    if live:
        command.add_argument(
            "--live",
            type=Path,
            help="the live database (default: FOOD_DATA_LIVE, else live.sqlite in FOOD_DATA_DIR,"
            " else data/live.sqlite)",
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
    build.add_argument(
        "--dist",
        type=Path,
        help="directory the database is written to (default: dist/ of the repository);"
        " another one leaves a database being served from dist/ alone",
    )
    build.add_argument(
        "--work",
        type=Path,
        help="scratch directory of the build (default: work/ of the repository)",
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

    serve = commands.add_parser("serve", help="answer the API over HTTP")
    _add_service_options(serve)
    serve.add_argument("--host", help=f"address to listen on (default {DEFAULT_HOST})")
    serve.add_argument("--port", type=int, help=f"port to listen on (default {DEFAULT_PORT})")
    serve.add_argument(
        "--user-version-votes",
        type=int,
        help="votes a version coming from a user needs to be served instead of a record of a"
        f" source (default: FOOD_DATA_USER_VERSION_VOTES, else {DEFAULT_USER_VERSION_VOTES})",
    )

    layer_out = commands.add_parser(
        "export-layer", help="write the public kratheos.sqlite from the live database"
    )
    layer_out.add_argument("--live", type=Path, help="the live database")
    layer_out.add_argument(
        "--out", type=Path, help="file to write (default: kratheos.sqlite of the repository)"
    )
    layer_out.add_argument(
        "--commit", action="store_true", help="commit the file, alone, with a dated message"
    )
    layer_out.add_argument(
        "--push", action="store_true", help="push after the commit, when a remote exists"
    )
    layer_out.add_argument(
        "--force",
        action="store_true",
        help="write even when an empty live database would empty a layer holding data",
    )

    layer_in = commands.add_parser(
        "import-layer", help="take over into the live database what a kratheos.sqlite holds"
    )
    layer_in.add_argument(
        "file", type=Path, nargs="?", help="default: kratheos.sqlite of the repository"
    )
    layer_in.add_argument("--live", type=Path, help="the live database")

    again = commands.add_parser(
        "rebuild", help="export, rebuild, compare with the database served, then swap it in"
    )
    _add_input_options(again)
    _add_service_options(again)
    again.add_argument(
        "--no-export",
        action="store_true",
        help="build with kratheos.sqlite as it is, without exporting the live database first",
    )
    again.add_argument(
        "--max-loss",
        type=float,
        default=maintenance.DEFAULT_MAX_LOSS,
        help="largest share of the foods being served that may disappear"
        f" (default {maintenance.DEFAULT_MAX_LOSS})",
    )

    publish = commands.add_parser(
        "database-files", help="lay out what to publish so that others can install the database"
    )
    _add_service_options(publish, live=False)
    publish.add_argument(
        "--out", type=Path, help="destination directory (default: release-database/)"
    )

    install = commands.add_parser(
        "install-database", help="download a ready-made database, verify it and swap it in"
    )
    install.add_argument("--url", required=True, help="address the database was published under")
    _add_service_options(install)
    install.add_argument(
        "--max-loss",
        type=float,
        default=maintenance.DEFAULT_MAX_LOSS,
        help="largest share of the foods being served that may disappear"
        f" (default {maintenance.DEFAULT_MAX_LOSS})",
    )
    install.add_argument(
        "--force", action="store_true", help="install even when the comparison says no"
    )
    install.add_argument(
        "--min-free-gb",
        type=float,
        default=DEFAULT_MIN_FREE_GB,
        help=f"stop rather than leave less free disk space (default {DEFAULT_MIN_FREE_GB:g})",
    )
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
        user_version_votes=_settings(args, Path.cwd(), with_secret=False).user_version_votes,
    )


def _settings(args: argparse.Namespace, root: Path, with_secret: bool = True) -> Settings:
    """Read the settings of the service for a command.

    Args:
        args: The parsed arguments.
        root: Directory of the repository.
        with_secret: Read, or create, the secret of the contributor keys.

    Returns:
        The settings: options first, then the environment, then the defaults.
    """
    layer = root / kratheos.FILE_NAME
    return load_settings(
        root,
        database=getattr(args, "database", None),
        live=getattr(args, "live", None),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        user_version_votes=getattr(args, "user_version_votes", None),
        layer=layer if layer.exists() else None,
        with_secret=with_secret,
    )


def _run_service_command(args: argparse.Namespace, workspace: Workspace, root: Path) -> int:
    """Run one of the commands of the service.

    Args:
        args: The parsed arguments.
        workspace: The directories of the repository.
        root: Directory of the repository.

    Returns:
        The process exit status.
    """
    if args.command == "serve":
        httpd.serve(_settings(args, root))
    elif args.command == "export-layer":
        settings = _settings(args, root, with_secret=False)
        out = (args.out or workspace.kratheos).resolve()
        summary = export.export_layer(settings.live, out, force=args.force)
        state = "written" if summary["changed"] else "unchanged"
        print(
            f"{out}: {state}; {summary['products']} products, {summary['versions']} versions,"
            f" {summary['votes']} votes on {summary['voted_versions']} versions,"
            f" {summary['translations']} names, {summary['portions']} portions;"
            f" sha256 {summary['sha256']}"
        )
        if args.commit or args.push:
            export.commit_layer(out, summary, push=args.push, log=_log)
    elif args.command == "import-layer":
        settings = _settings(args, root)
        layer = kratheos.read_layer((args.file or workspace.kratheos).resolve())
        store = LiveStore(settings.live, settings.key_secret)
        try:
            added = store.import_layer(layer)
        finally:
            store.close()
        print(
            f"{settings.live}: took over {added['products']} products, {added['versions']}"
            f" versions, {added['translations']} names, {added['portions']} portions and"
            f" {added['votes']} votes"
        )
    elif args.command == "rebuild":
        settings = _settings(args, root, with_secret=False)
        try:
            maintenance.rebuild(
                workspace,
                _options(args),
                settings.database,
                None if args.no_export else settings.live,
                max_loss=args.max_loss,
                log=_log,
            )
        except maintenance.Refused as refusal:
            print(f"refused, the database being served stays: {refusal}", file=sys.stderr)
            return EXIT_REFUSED
    elif args.command == "database-files":
        settings = _settings(args, root, with_secret=False)
        out_dir = (args.out or root / "release-database").resolve()
        layer = workspace.kratheos if workspace.kratheos.exists() else None
        for entry in maintenance.database_files(settings.database, layer, out_dir, log=_log):
            print(f"{entry['size']:>14,}  {entry['sha256']}  {entry['asset']}")
        print(f"publish the files of {out_dir} under one address")
    elif args.command == "install-database":
        settings = _settings(args, root)
        try:
            maintenance.install_database(
                args.url,
                settings.database,
                settings.live,
                settings.key_secret,
                max_loss=args.max_loss,
                force=args.force,
                min_free_gb=args.min_free_gb,
                log=_log,
            )
        except maintenance.Refused as refusal:
            print(f"refused, the database being served stays: {refusal}", file=sys.stderr)
            return EXIT_REFUSED
    return 0


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
            where = dataclasses.replace(
                workspace,
                dist=args.dist.resolve() if args.dist else workspace.dist,
                work=args.work.resolve() if args.work else workspace.work,
            )
            run_build(where, _options(args), log=_log)
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
        else:
            return _run_service_command(args, workspace, root)
    except (
        ConfigError,
        DownloadError,
        DiskSpaceError,
        FileNotFoundError,
        kratheos.LayerError,
        SettingsError,
        export.ExportRefused,
        RuntimeError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
