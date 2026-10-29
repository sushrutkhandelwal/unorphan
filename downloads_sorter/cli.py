"""Command line: preview, --apply, --undo, --list-projects, and `ui`."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import sys
from pathlib import Path

from . import core
from .core import APP, CONFIG_PATH, FOLDERS_PATH, PRICE_PER_MTOK, SorterError


def short(name: str, width: int = 58) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog=APP, description="Sort loose files in Downloads into project folders using Jev.")
    ap.add_argument("command", nargs="?", choices=["ui"], help="'ui' opens the web interface")
    ap.add_argument("--apply", action="store_true", help="actually move files (default is a preview)")
    ap.add_argument("--undo", action="store_true", help="put back everything moved by the last move")
    ap.add_argument("--list-projects", action="store_true", help="show every folder and what Jev is told about it")
    ap.add_argument("--downloads", help="the folder to organize (default ~/Downloads)")
    ap.add_argument("--dest", help="where folders are created (default <downloads>/projects)")
    ap.add_argument("--projects-root", action="append", help="folder holding your projects; repeatable")
    ap.add_argument("--exclude-project", action="append", help="project folder name to leave out; repeatable")
    ap.add_argument("--threshold", type=float, help="minimum confidence to move, 0-1 (default 0.8)")
    ap.add_argument("--image-max-side", type=int, help="longest side of images sent to Jev, px (default 768)")
    ap.add_argument("--image-quality", type=int, help="JPEG quality of images sent to Jev (default 60)")
    ap.add_argument("--no-images", action="store_true", help="send names only, never pictures")
    ap.add_argument("--concurrency", type=int, help="parallel requests (default 6)")
    ap.add_argument("--env-file", help=".env file to read TYPESAFE_API_KEY or JEV_API_KEY from")
    ap.add_argument("--only", help="only consider Downloads items matching this glob, e.g. '*.png'")
    ap.add_argument("--limit", type=int, help="only consider the N most recent items")
    ap.add_argument("--no-cache", action="store_true", help="re-ask Jev even for items it has already seen")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    ap.add_argument("--port", type=int, default=0, help="port for the ui (default: any free port)")
    ap.add_argument("--no-browser", action="store_true", help="ui: don't open a browser tab")
    ap.add_argument("--config", help=f"config file (default {CONFIG_PATH})")
    return ap.parse_args(argv)


def overrides_from_args(args: argparse.Namespace) -> dict:
    return {
        "downloads": args.downloads,
        "dest": args.dest,
        "threshold": args.threshold,
        "image_max_side": args.image_max_side,
        "image_quality": args.image_quality,
        "concurrency": args.concurrency,
        "env_file": args.env_file,
        "project_roots": args.projects_root,
        "send_images": False if args.no_images else None,
    }


def config_path(args: argparse.Namespace) -> Path | None:
    return Path(args.config).expanduser() if args.config else None


def config_from_args(args: argparse.Namespace) -> dict:
    cfg = core.load_config(config_path(args), overrides_from_args(args))
    if args.exclude_project:
        cfg["exclude_projects"] = list(cfg["exclude_projects"]) + args.exclude_project
    return cfg


def run(args: argparse.Namespace) -> None:
    if args.command == "ui":  # the UI opens even if setup isn't finished, and helps finish it
        from .ui import serve

        serve(config_path(args), overrides_from_args(args), port=args.port, open_browser=not args.no_browser)
        return

    cfg = config_from_args(args)

    if args.undo:
        print(core.undo_last(cfg))
        return

    folders = core.all_folders(cfg)
    if args.list_projects:
        for f in folders:
            where = "custom folder" if f.custom else ", ".join(str(p) for p in f.paths)
            off = "  [switched off]" if not f.enabled else ""
            print(f"\n{f.name}  ({where}){off}\n  {f.text()}")
        print(f"\n{len(folders)} folders. Custom folders and hints live in {FOLDERS_PATH} (edit them in the ui).")
        return

    paths = core.list_items(cfg)
    if args.only:
        paths = [p for p in paths if fnmatch.fnmatch(p.name, args.only)]
    if args.limit:
        paths = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]
    if not paths:
        print("Nothing loose in Downloads to sort.")
        return

    api_key = core.resolve_api_key(cfg)
    enabled = [f for f in folders if f.enabled]
    print(f"Checking {len(paths)} item(s) in {cfg['downloads']} against {len(enabled)} folders…", file=sys.stderr)
    items = [core.build_item(p, cfg) for p in paths]

    def progress(done: int, total: int) -> None:
        if sys.stderr.isatty():
            print(f"\r  classifying {done}/{total}", end="" if done < total else "\n", file=sys.stderr, flush=True)

    tokens = asyncio.run(core.classify_all(items, folders, cfg, api_key, read_cache=not args.no_cache, progress=progress))
    if tokens:
        print(f"  Jev usage: {tokens:,} input tokens, about ${tokens / 1e6 * PRICE_PER_MTOK:.3f} (cached items are free)", file=sys.stderr)

    threshold = float(cfg["threshold"])
    decided = [(it, *core.decide(it, threshold)) for it in items]
    moves = [(it.path, name) for it, name, _ in decided if name]

    if args.json:
        print(json.dumps([{"item": it.path.name, "move_to": name, "reason": why, **it.result} for it, name, why in decided], indent=2))
    else:
        by_folder: dict[str, list[tuple[core.Item, str]]] = {}
        for it, name, why in decided:
            if name:
                by_folder.setdefault(name, []).append((it, why))
        print(f"\n{'Moving' if args.apply else 'Would move'} {len(moves)} item(s) into {cfg['dest']}/<folder>:")
        for name in sorted(by_folder, key=str.lower):
            print(f"\n  {name}/")
            for it, why in by_folder[name]:
                pic = " [picture]" if it.result.get("used_image") else ""
                print(f"    {short(it.path.name):<58} {why:>5}{pic}")
        kept = sorted(((it, why) for it, name, why in decided if not name), key=lambda x: x[1])
        print(f"\nLeaving {len(kept)} item(s) in place:")
        for it, why in kept:
            print(f"    {short(it.path.name):<58} {why}")

    if args.apply and moves:
        moved, errors = core.apply_moves(moves, cfg)
        for e in errors:
            print(f"  could not move {e}")
        print(f"\nMoved {len(moved)} item(s) into {cfg['dest']}. Undo with: {APP} --undo")
    elif moves and not args.json:
        print(f"\nPreview only, nothing was moved. Run again with --apply to move these {len(moves)} item(s).")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        run(args)
    except SorterError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
