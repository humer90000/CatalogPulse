from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from catalogpulse.config import ConfigError, load_site
from catalogpulse.crawl import CrawlError, execute, report_run
from catalogpulse.save import Store

log = logging.getLogger("catalogpulse")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _logging(bool(getattr(args, "verbose", False)))
    try:
        if args.cmd == "run":
            return _run(args)
        if args.cmd == "show":
            return _show(args)
        if args.cmd == "export":
            return _export(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except CrawlError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("stopped")
        return 130
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catalogpulse",
        description="Crawl a catalogue, keep every snapshot, print what moved.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="data/catalog.db", help="sqlite file (default: data/catalog.db)")
    common.add_argument("-v", "--verbose", action="store_true")

    files = argparse.ArgumentParser(add_help=False)
    files.add_argument("--out", default="exports", help="export directory (default: exports)")
    files.add_argument(
        "--format",
        nargs="+",
        choices=("json", "csv", "xlsx"),
        default=["json", "csv", "xlsx"],
        help="json, csv, xlsx (default: all three)",
    )

    run = sub.add_parser("run", parents=[common, files], help="crawl one or more site configs")
    run.add_argument("targets", nargs="+", help="yaml files, or a directory of them")

    show = sub.add_parser("show", parents=[common], help="print the latest stored comparison")
    show.add_argument("site", help="site name from the yaml")

    export = sub.add_parser("export", parents=[common, files], help="rewrite the files from the database")
    export.add_argument("site", help="site name from the yaml")
    return parser


def _run(args) -> int:
    code = 0
    configs = _configs(args.targets)
    formats = set(args.format)
    for index, path in enumerate(configs):
        if index:
            say("")
        try:
            site = load_site(path)
            status, text = asyncio.run(execute(site, Path(args.db), Path(args.out), formats))
        except (ConfigError, CrawlError) as exc:
            log.error("%s  %s", path.name, exc)
            code = max(code, 2 if isinstance(exc, ConfigError) else 1)
            continue
        if text:
            say(text)
        code = max(code, status)
    return code


def _show(args) -> int:
    store = Store(Path(args.db))
    try:
        text = report_run(store, args.site, None, set())
    finally:
        store.close()
    say(text)
    return 0


def _export(args) -> int:
    store = Store(Path(args.db))
    try:
        text = report_run(store, args.site, Path(args.out), set(args.format))
    finally:
        store.close()
    say(text)
    return 0


def _configs(targets: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in targets:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(path.glob("*.yaml")))
            found.extend(sorted(path.glob("*.yml")))
        elif path.is_file():
            found.append(path)
        else:
            raise ConfigError(f"no such file: {path}")
    uniq: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    if not uniq:
        raise ConfigError("no yaml configs in that path")
    return uniq


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def say(text: str) -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    payload = text.rstrip() + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        print(payload, end="")
        return
    buffer.write(payload.encode(encoding, errors="replace"))
    buffer.flush()
