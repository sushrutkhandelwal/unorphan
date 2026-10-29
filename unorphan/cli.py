"""The unorphan command: preview / --apply / --undo, plus ui, status, config, folders and profiles."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import sys
from pathlib import Path

from . import core
from .core import APP, PRICE_PER_MTOK, SorterError, pretty

LIST_KEYS = {"project_roots", "ignore"}
BOOL_KEYS = {"send_images", "nested_projects"}
TRUE, FALSE = {"true", "yes", "on", "1"}, {"false", "no", "off", "0"}


def short(name: str, width: int = 58) -> str:
    return name if len(name) <= width else name[: width - 1] + "…"


# --------------------------------------------------------------------------- arguments


def add_common(p: argparse.ArgumentParser, suppress: bool) -> None:
    """Options every command accepts, before or after the command name."""
    d = {"default": argparse.SUPPRESS} if suppress else {}
    p.add_argument("--profile", "-p", help="use this profile instead of the active one", **d)
    p.add_argument("--config", help="use this config file instead of a profile", **d)
    p.add_argument("--folder", "--downloads", dest="folder", help="the folder to organize, for this run", **d)
    p.add_argument("--dest", help="where sorted files go, for this run", **d)
    p.add_argument("--projects-root", action="append", help="folder holding your projects; repeatable", **d)
    p.add_argument("--exclude-project", action="append", help="project folder name to leave out; repeatable", **d)
    p.add_argument("--threshold", type=float, help="minimum confidence to move, 0-1 (default 0.8)", **d)
    p.add_argument("--image-max-side", type=int, help="longest side of images sent to Jev, px (default 768)", **d)
    p.add_argument("--image-quality", type=int, help="JPEG quality of images sent to Jev (default 60)", **d)
    p.add_argument("--no-images", action="store_true", help="send names only, never pictures", **d)
    p.add_argument("--concurrency", type=int, help="parallel requests (default 6)", **d)
    p.add_argument("--env-file", help=".env file to read TYPESAFE_API_KEY or JEV_API_KEY from", **d)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog=APP,
        description="Find a home for the loose files in a folder. With no command: show what would move.",
        epilog="Run `unorphan <command> --help` for each command's options.",
    )
    add_common(ap, suppress=False)
    ap.add_argument("--apply", action="store_true", help="actually move files (default is a preview)")
    ap.add_argument("--undo", action="store_true", help="put back everything moved by the last move")
    ap.add_argument("--list-projects", action="store_true", help="show every destination and what Jev is told about it")
    ap.add_argument("--only", help="only consider items matching this glob, e.g. '*.png'")
    ap.add_argument("--limit", type=int, help="only consider the N most recent items")
    ap.add_argument("--no-cache", action="store_true", help="re-ask Jev even for items it has already seen")
    ap.add_argument("--json", action="store_true", help="print results as JSON")

    sub = ap.add_subparsers(dest="command", metavar="command")

    ui = sub.add_parser("ui", help="open the app in your browser")
    add_common(ui, suppress=True)
    ui.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    ui.add_argument("--no-browser", action="store_true", help="don't open a browser tab")

    st = sub.add_parser("status", help="show the setup and anything missing (exit code 1 if not ready)")
    add_common(st, suppress=True)
    st.add_argument("--json", action="store_true", help="print as JSON")

    cf = sub.add_parser("config", help="show or change settings",
                        description="With no action, show the settings. Keys: " + ", ".join(sorted(core.EDITABLE)))
    add_common(cf, suppress=True)
    cf_sub = cf.add_subparsers(dest="action", metavar="action")
    cs = cf_sub.add_parser("set", help="change a setting, e.g. `config set project_roots ~/code ~/work`")
    cs.add_argument("key", choices=sorted(core.EDITABLE))
    cs.add_argument("values", nargs="+", help="for api_key, pass - and you'll be asked for it (or pipe it in)")
    cu = cf_sub.add_parser("unset", help="go back to the default for these settings")
    cu.add_argument("keys", nargs="+")

    fo = sub.add_parser("folders", help="list, add or remove your own folders; hint or switch off projects")
    add_common(fo, suppress=True)
    fo_sub = fo.add_subparsers(dest="action", metavar="action")
    fa = fo_sub.add_parser("add", help="add a folder: NAME \"what goes in it\"")
    fa.add_argument("name"); fa.add_argument("description")
    fr = fo_sub.add_parser("remove", help="remove one of your folders")
    fr.add_argument("name")
    fh = fo_sub.add_parser("hint", help="tell Jev more about a project: PROJECT \"hint\" (empty text removes it)")
    fh.add_argument("name"); fh.add_argument("text")
    for verb, text in (("off", "never file anything into this project"), ("on", "let Jev file into this project again")):
        fx = fo_sub.add_parser(verb, help=text)
        fx.add_argument("name")

    pr = sub.add_parser("profiles", help="list, add, switch, rename or remove profiles")
    pr_sub = pr.add_subparsers(dest="action", metavar="action")
    pa = pr_sub.add_parser("add", help="create a profile")
    pa.add_argument("name")
    pa.add_argument("--copy-from", metavar="PROFILE", help="start with a copy of this profile's settings and folders")
    pu = pr_sub.add_parser("use", help="make a profile the active one")
    pu.add_argument("name")
    pn = pr_sub.add_parser("rename", help="rename a profile")
    pn.add_argument("old"); pn.add_argument("new")
    pd = pr_sub.add_parser("remove", help="delete a profile and its settings (your files aren't touched)")
    pd.add_argument("name")

    args = ap.parse_args(argv)
    for name in ("profile", "config", "folder", "dest", "projects_root", "exclude_project", "threshold",
                 "image_max_side", "image_quality", "concurrency", "env_file"):
        if not hasattr(args, name):
            setattr(args, name, None)
    for name in ("no_images", "json"):
        if not hasattr(args, name):
            setattr(args, name, False)
    return args


def overrides_from_args(args: argparse.Namespace) -> dict:
    return {
        "folder": args.folder,
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


def config_from_args(args: argparse.Namespace, validate: bool = True) -> dict:
    cfg = core.load_config(config_path(args), overrides_from_args(args), validate=validate, profile=args.profile)
    if args.exclude_project:
        cfg["exclude_projects"] = list(cfg["exclude_projects"]) + args.exclude_project
    return cfg


# --------------------------------------------------------------------------- commands


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_from_args(args, validate=False)
    problems = core.setup_problems(cfg)
    key, where = core.find_api_key(cfg)
    roots = core.root_report(cfg)
    settings = core.load_folder_settings(cfg["folders_path"])
    loose = len(core.list_items(cfg)) if cfg["folder"].is_dir() else 0
    info = {
        "profile": cfg["profile"],
        "profiles": core.list_profiles(),
        "config_file": pretty(cfg["config_path"]),
        "folders_file": pretty(cfg["folders_path"]),
        "organizing": pretty(cfg["folder"]),
        "loose_items": loose,
        "sorted_into": pretty(cfg["dest"]),
        "project_roots": [{"path": r["path"], "exists": r["exists"], "projects": len(r["projects"]), "auto_detected": r["auto"]} for r in roots],
        "own_folders": [c["name"] for c in settings["custom"]],
        "api_key": {"found": bool(key), "source": where},
        "threshold": float(cfg["threshold"]),
        "send_images": bool(cfg["send_images"]),
        "ready": not problems,
        "problems": problems,
    }
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        rows = [
            ("Profile", f"{cfg['profile']}" + (f"  (also: {', '.join(n for n in info['profiles'] if n != cfg['profile'])})" if len(info["profiles"]) > 1 else "")),
            ("Organizing", f"{info['organizing']}  ({loose} loose item{'s' if loose != 1 else ''})"),
            ("Sorted into", info["sorted_into"]),
            ("Projects", ", ".join(f"{r['path']} ({len(r['projects'])})" + (" [auto]" if r["auto"] else "") + ("" if r["exists"] else " [missing]") for r in roots) or "none"),
            ("Own folders", ", ".join(info["own_folders"]) or "none"),
            ("Jev key", ("found: " if key else "missing: ") + where),
            ("Settings", info["config_file"]),
        ]
        for label, value in rows:
            print(f"{label:<12} {value}")
        print("\nReady." if not problems else "\nNeeds attention:\n" + "\n".join(f"  - {p}" for p in problems))
    return 0 if not problems else 1


def parse_value(key: str, values: list[str]):
    if key == "api_key":
        if values != ["-"]:
            raise SorterError("Don't put the key on the command line (it ends up in your shell history). "
                              "Run `unorphan config set api_key -` and paste it when asked.")
        if sys.stdin.isatty():
            import getpass
            raw = getpass.getpass("Paste your Jev API key (it won't be shown), then press Enter: ")
        else:
            raw = sys.stdin.read()  # piped in, e.g. from a password manager
        if not raw.strip():
            raise SorterError("No key given, nothing changed.")
        return raw.strip()
    if key in LIST_KEYS:
        return values
    if len(values) != 1:
        raise SorterError(f"{key} takes one value.")
    v = values[0]
    if key in BOOL_KEYS:
        if v.lower() not in TRUE | FALSE:
            raise SorterError(f"{key} must be true or false.")
        return v.lower() in TRUE
    if key == "threshold":
        t = float(v.rstrip("%"))
        return t / 100 if t > 1 else t  # accept 80, 80% or 0.8
    if key == "image_max_side":
        return int(v)
    return v


def cmd_config(args: argparse.Namespace) -> int:
    cfg = config_from_args(args, validate=False)
    path = cfg["config_path"]
    if args.action == "set":
        value = parse_value(args.key, args.values)
        try:
            saved = core.save_config({args.key: value}, path)
        except core.SettingsError as e:
            raise SorterError("; ".join(f"{k}: {v}" for k, v in e.errors.items())) from e
        shown = "(hidden)" if args.key == "api_key" else json.dumps(saved.get(args.key, value))
        print(f"Set {args.key} = {shown} in profile '{cfg['profile']}'.")
        return 0
    if args.action == "unset":
        core.unset_config(args.keys, path)
        print(f"Back to defaults: {', '.join(args.keys)} (profile '{cfg['profile']}').")
        return 0
    saved = core.read_config_file(path)
    print(f"# profile '{cfg['profile']}', saved in {pretty(path)}")
    for key in sorted(core.EDITABLE):
        val = cfg.get(key)
        if key == "api_key":
            val = "(saved)" if saved.get("api_key") else "(not saved here)"
        elif isinstance(val, Path):
            val = pretty(val)
        mark = "" if key in saved else "   # default"
        shown = val if key == "api_key" else json.dumps(val, default=str)
        print(f"{key} = {shown}{mark}")
    return 0


def cmd_folders(args: argparse.Namespace) -> int:
    cfg = config_from_args(args, validate=False)
    path = cfg["folders_path"]
    settings = core.load_folder_settings(path)
    if args.action == "add":
        if any(c["name"].lower() == args.name.lower() for c in settings["custom"]):
            raise SorterError(f"You already have a folder called '{args.name}'.")
        settings["custom"].append({"name": args.name, "description": args.description})
    elif args.action == "remove":
        before = len(settings["custom"])
        settings["custom"] = [c for c in settings["custom"] if c["name"].lower() != args.name.lower()]
        if len(settings["custom"]) == before:
            raise SorterError(f"There's no folder of yours called '{args.name}'.")
    elif args.action in ("hint", "off", "on"):
        projects = core.find_projects(cfg)
        if args.name not in projects:
            raise SorterError(f"There's no project called '{args.name}'. See `unorphan --list-projects`.")
        if args.action == "hint":
            settings["notes"][args.name] = args.text
        elif args.action == "off":
            settings["disabled"] = sorted(set(settings["disabled"]) | {args.name})
        else:
            settings["disabled"] = [d for d in settings["disabled"] if d != args.name]
    else:
        print(f"# profile '{cfg['profile']}', saved in {pretty(path)}")
        if not settings["custom"]:
            print("No folders of your own yet. Add one: unorphan folders add NAME \"what goes in it\"")
        for c in settings["custom"]:
            print(f"{c['name']}: {c['description']}")
        for name, note in settings["notes"].items():
            print(f"hint  {name}: {note}")
        for name in settings["disabled"]:
            print(f"off   {name}")
        return 0
    core.save_folder_settings(settings, path)
    print(f"Saved (profile '{cfg['profile']}'). Jev looks at every file again on the next scan.")
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    if args.action == "add":
        core.create_profile(args.name, args.copy_from)
        print(f"Created profile '{args.name}'." + (f" Copied from '{args.copy_from}'." if args.copy_from else "")
              + f" Switch to it with: {APP} profiles use {args.name}")
    elif args.action == "use":
        core.set_active_profile(args.name)
        print(f"Now using profile '{args.name}'.")
    elif args.action == "rename":
        core.rename_profile(args.old, args.new)
        print(f"Renamed '{args.old}' to '{args.new}'.")
    elif args.action == "remove":
        core.delete_profile(args.name)
        print(f"Deleted profile '{args.name}'. Active profile: {core.active_profile()}.")
    else:
        active = core.active_profile()
        for name in core.list_profiles():
            print(("* " if name == active else "  ") + name)
    return 0


def cmd_sort(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)

    if args.undo:
        print(core.undo_last(cfg))
        return 0

    folders = core.all_folders(cfg)
    if args.list_projects:
        for f in folders:
            where = "your folder" if f.custom else ", ".join(pretty(p) for p in f.paths)
            off = "  [switched off]" if not f.enabled else ""
            print(f"\n{f.name}  ({where}){off}\n  {f.text()}")
        print(f"\n{len(folders)} destinations in profile '{cfg['profile']}'.")
        return 0

    paths = core.list_items(cfg)
    if args.only:
        paths = [p for p in paths if fnmatch.fnmatch(p.name, args.only)]
    if args.limit:
        paths = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]
    if not paths:
        print(f"Nothing loose in {pretty(cfg['folder'])} to sort.")
        return 0

    api_key = core.resolve_api_key(cfg)
    enabled = [f for f in folders if f.enabled]
    print(f"Checking {len(paths)} item(s) in {pretty(cfg['folder'])} against {len(enabled)} destinations…", file=sys.stderr)
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
        print(f"\n{'Moving' if args.apply else 'Would move'} {len(moves)} item(s) into {pretty(cfg['dest'])}/<folder>:")
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
        print(f"\nMoved {len(moved)} item(s) into {pretty(cfg['dest'])}. Undo with: {APP} --undo")
    elif moves and not args.json:
        print(f"\nPreview only, nothing was moved. Run again with --apply to move these {len(moves)} item(s).")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.command == "ui":  # the app opens even if setup isn't finished, and helps finish it
        from .ui import serve

        serve(config_path(args), overrides_from_args(args), profile=args.profile, port=args.port, open_browser=not args.no_browser)
        return 0
    return {"status": cmd_status, "config": cmd_config, "folders": cmd_folders, "profiles": cmd_profiles}.get(args.command, cmd_sort)(args)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        code = run(args)
    except SorterError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(code)


if __name__ == "__main__":
    main()
