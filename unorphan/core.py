"""Finding destinations, describing Downloads items, asking Jev, moving, undoing.

Destinations ("folders") are your code projects plus any custom folders you
define, such as a folder for a client you work with. Each loose item in Downloads is
described to Jev (TypeSafe's classification model) by its name, plus a shrunk,
re-compressed copy if it is an image. Jev picks the folder it belongs in.
Items are moved to <Downloads>/projects/<folder>/. Your actual project folders
are only read, never written to.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

APP = "unorphan"
MODEL = "jev-latest"
PRICE_PER_MTOK = 0.042  # USD per million input tokens; output is free (docs.typesafe.ai/models)
NO_PROJECT = "no_project"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
PROFILES_DIR = CONFIG_DIR / "profiles"  # one folder per profile: config.toml + folders.json
STATE_PATH = CONFIG_DIR / "state.json"  # which profile is active
DEFAULT_PROFILE = "default"
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
CACHE_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP / "cache.json"
LOG_NAME = ".unorphan-log.jsonl"

# Jev rejects images with a side under 280px or over 1568px.
JEV_MIN_SIDE, JEV_MAX_SIDE = 280, 1568

DEFAULTS = {
    "downloads": "~/Downloads",  # the folder to organize
    "dest": None,  # where sorted files go; defaults to <downloads>/projects
    "project_roots": [],  # folders holding your projects; empty = auto-detect common locations
    "nested_projects": False,  # also look one level into folders that only hold projects
    "exclude_projects": [],
    "project_notes": {},  # extra hints per project
    "ignore": [],  # glob patterns of items to never touch
    "threshold": 0.8,
    "image_max_side": 768,
    "image_quality": 60,
    "send_images": True,
    "concurrency": 6,
    "env_file": None,  # a .env file to read TYPESAFE_API_KEY / JEV_API_KEY from
    "api_key": None,  # the key itself, if you'd rather keep it here (the file is made private)
}
# Settings the UI can change, with how to clean each one up.
EDITABLE = {"downloads", "dest", "project_roots", "nested_projects", "ignore", "threshold", "send_images", "image_max_side", "env_file", "api_key"}

AUTO_ROOTS = ["~/dev", "~/code", "~/projects", "~/src", "~/Developer", "~/workspace", "~/repos", "~/git"]
PROJECT_MARKERS = [
    ".git", "package.json", "pyproject.toml", "requirements.txt", "CLAUDE.md", "AGENTS.md",
    "README.md", "Cargo.toml", "go.mod", "Gemfile", "pom.xml", "build.gradle", "index.html",
]
# Read in this order; each useful one adds a snippet. READMEs say what a project
# is, CLAUDE.md / AGENTS.md mostly say how to work on it, so they come last.
DESC_FILES = [".claude/project-summary.md", "README.md", "README.en.md", "readme.md", "CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md"]
# Files that came from a template or tool and say nothing about the project.
BOILERPLATE = [
    re.compile(r"bootstrapped with \[?`?create-next-app", re.I),
    re.compile(r"you have now installed the Sanity Content Studio", re.I),
    re.compile(r"This template provides a minimal setup", re.I),
    re.compile(r"^#\s*HyperFrames Composition Project", re.I | re.M),
]
GENERATED_BLOCK = re.compile(r"<!-- BEGIN:([\w-]+) -->.*?<!-- END:\1 -->", re.S)  # e.g. Next.js agent rules
SKIP_NAMES = {".DS_Store", ".localized", "desktop.ini", "Thumbs.db"}
PARTIAL_SUFFIXES = (".crdownload", ".download", ".part", ".partial", ".tmp")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
KINDS = {
    "image": IMAGE_EXTS | {".svg", ".heic", ".avif", ".ico"},
    "video": {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    "pdf": {".pdf"},
    "spreadsheet": {".xlsx", ".xls", ".csv", ".tsv", ".numbers", ".ods"},
    "document": {".doc", ".docx", ".md", ".txt", ".rtf", ".pages", ".odt", ".pptx", ".ppt", ".key"},
    "web page": {".html", ".htm"},
    "archive": {".zip", ".tar", ".gz", ".tgz", ".rar", ".7z"},
    "installer": {".dmg", ".pkg", ".exe", ".msi", ".app", ".deb", ".appimage"},
    "code or config": {".py", ".js", ".ts", ".json", ".sh", ".yaml", ".yml", ".toml", ".sql"},
}
FOLDER_NAME = re.compile(r"^[^/\\:\x00-\x1f]{1,60}$")


class SorterError(Exception):
    """A problem the user can fix (bad config, missing key, ...)."""


# --------------------------------------------------------------------------- profiles
#
# A profile is a folder holding config.toml (settings) and folders.json (your
# own folders, hints, switched-off projects). Profiles let one person organize
# several folders differently, e.g. Downloads against their code, and Desktop
# against their clients.


def _migrate_single_config() -> None:
    """Older versions kept config.toml and folders.json directly in CONFIG_DIR."""
    old = [CONFIG_DIR / "config.toml", CONFIG_DIR / "folders.json"]
    if PROFILES_DIR.exists() or not any(f.exists() for f in old):
        return
    target = PROFILES_DIR / DEFAULT_PROFILE
    target.mkdir(parents=True)
    for f in old:
        if f.exists():
            f.rename(target / f.name)


def list_profiles() -> list[str]:
    _migrate_single_config()
    names = sorted((d.name for d in PROFILES_DIR.iterdir() if d.is_dir()), key=str.lower) if PROFILES_DIR.exists() else []
    return names or [DEFAULT_PROFILE]


def active_profile() -> str:
    """UNORPHAN_PROFILE, else the one last chosen, else the first there is."""
    names = list_profiles()
    wanted = os.environ.get("UNORPHAN_PROFILE")
    if not wanted and STATE_PATH.exists():
        try:
            wanted = json.loads(STATE_PATH.read_text()).get("active")
        except (json.JSONDecodeError, AttributeError):
            wanted = None
    if wanted:
        check_profile_name(wanted)
        return wanted
    return names[0]


def check_profile_name(name: str) -> str:
    if not PROFILE_NAME.match(name or ""):
        raise SorterError(f"'{name}' can't be a profile name. Use letters, numbers, dots, dashes or underscores (up to 40).")
    return name


def profile_dir(name: str) -> Path:
    return PROFILES_DIR / check_profile_name(name)


def set_active_profile(name: str) -> None:
    if name not in list_profiles():
        raise SorterError(f"There's no profile called '{name}'. Profiles: {', '.join(list_profiles())}")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"active": name}))


def create_profile(name: str, copy_from: str | None = None) -> None:
    target = profile_dir(name)
    if name.lower() in (n.lower() for n in list_profiles()) and target.exists():
        raise SorterError(f"There's already a profile called '{name}'.")
    if copy_from:
        src = profile_dir(copy_from)
        if not src.exists():
            raise SorterError(f"There's no profile called '{copy_from}' to copy.")
        shutil.copytree(src, target)
    else:
        target.mkdir(parents=True)


def rename_profile(old: str, new: str) -> None:
    src, dst = profile_dir(old), profile_dir(new)
    if not src.exists():
        raise SorterError(f"There's no profile called '{old}'.")
    if dst.exists():
        raise SorterError(f"There's already a profile called '{new}'.")
    src.rename(dst)
    if active_profile() == old:
        set_active_profile(new)


def delete_profile(name: str) -> None:
    names = list_profiles()
    if name not in names or not profile_dir(name).exists():
        raise SorterError(f"There's no profile called '{name}'.")
    if len(names) == 1:
        raise SorterError("That's the only profile, so it can't be deleted.")
    was_active = active_profile() == name
    shutil.rmtree(profile_dir(name))
    if was_active:
        set_active_profile(next(n for n in names if n != name))


# --------------------------------------------------------------------------- config


def read_config_file(path: Path, must_exist: bool = False) -> dict:
    """The raw settings saved in config.toml (without defaults)."""
    if not path.exists():
        if must_exist:
            raise SorterError(f"config file not found: {path}")
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SorterError(f"{path} isn't valid TOML: {e}") from e


def load_config(
    path: Path | None = None,
    overrides: dict | None = None,
    validate: bool = True,
    profile: str | None = None,
) -> dict:
    """Settings for a profile (the active one by default), or from an explicit config file."""
    explicit = path is not None
    if explicit:
        name, folder = path.stem, path.parent  # a one-off config file; its folders.json sits next to it
    else:
        name = profile or active_profile()
        if profile and name not in list_profiles():
            raise SorterError(f"There's no profile called '{name}'. Profiles: {', '.join(list_profiles())}")
        folder = profile_dir(name)
        path = folder / "config.toml"
    cfg = dict(DEFAULTS)
    cfg.update(read_config_file(path, must_exist=explicit))
    for key, val in (overrides or {}).items():
        if val is not None:
            cfg[key] = val
    cfg["profile"] = name
    cfg["config_path"] = path
    cfg["folders_path"] = folder / "folders.json"
    cfg["downloads"] = Path(cfg["downloads"]).expanduser()
    dest = Path(cfg["dest"]).expanduser() if cfg["dest"] else Path("projects")
    cfg["dest"] = dest if dest.is_absolute() else cfg["downloads"] / dest  # relative = inside the organized folder
    if validate:
        problems = config_problems(cfg)
        if problems:
            raise SorterError(problems[0])
    return cfg


def config_problems(cfg: dict) -> list[str]:
    """Everything wrong with a config, in plain words. Empty if it's usable."""
    problems = []
    if not cfg["downloads"].is_dir():
        problems.append(f"The folder to organize doesn't exist: {cfg['downloads']}")
    if not JEV_MIN_SIDE <= int(cfg["image_max_side"]) <= JEV_MAX_SIDE:
        problems.append(f"Picture size must be between {JEV_MIN_SIDE} and {JEV_MAX_SIDE} px")
    if not 0 < float(cfg["threshold"]) <= 1:
        problems.append("The confidence threshold must be between 0 and 1, e.g. 0.8")
    return problems


def find_api_key(cfg: dict) -> tuple[str | None, str]:
    """(key or None, where it came from, in words)."""
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY"):
        if os.environ.get(name):
            return os.environ[name], f"the {name} environment variable"
    if cfg.get("api_key"):
        return str(cfg["api_key"]), "your settings"
    if cfg.get("env_file"):
        env = Path(cfg["env_file"]).expanduser()
        if env.is_file():
            for line in env.read_text().splitlines():
                m = re.match(r"\s*(?:export\s+)?(TYPESAFE_API_KEY|JEV_API_KEY)\s*=\s*(.*)$", line)
                if m and m.group(2).strip().strip("'\""):
                    return m.group(2).strip().strip("'\""), f"{m.group(1)} in {pretty(env)}"
            return None, f"{pretty(env)} has no TYPESAFE_API_KEY or JEV_API_KEY line"
        return None, f"{pretty(env)} doesn't exist"
    return None, "not set"


def resolve_api_key(cfg: dict) -> str:
    key, where = find_api_key(cfg)
    if key:
        return key
    raise SorterError(
        f"No Jev API key found ({where}). Get one at https://console.typesafe.ai/keys, then add it in the\n"
        f"app's Settings, run `unorphan config set api_key -` and paste it, or set TYPESAFE_API_KEY."
    )


def pretty(path: Path | str) -> str:
    """A path with your home folder shown as ~."""
    p, home = str(path), str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home + os.sep) else p


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # JSON string escapes are valid TOML
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(str(k))} = {_toml(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"can't save {value!r}")


def save_config(updates: dict, path: Path) -> dict:
    """Clean up and save settings from the UI or `unorphan config set`. Keeps settings it doesn't know about."""
    data = read_config_file(path)
    errors: dict[str, str] = {}
    for key, val in updates.items():
        if key not in EDITABLE:
            continue
        if key in ("downloads", "dest", "env_file"):
            val = str(val or "").strip()
            if not val:
                data.pop(key, None)
                continue
            p = Path(val).expanduser()
            if key == "dest" and not p.is_absolute():
                data[key] = val  # relative: a folder inside the one being organized
                continue
            if key == "downloads" and not p.is_dir():
                errors[key] = "That folder doesn't exist."
            elif key == "env_file" and not p.is_file():
                errors[key] = "That file doesn't exist."
            elif key == "dest" and p.exists() and not p.is_dir():
                errors[key] = "That's a file, not a folder."
            data[key] = pretty(p.resolve() if p.exists() else p)
        elif key == "project_roots":
            roots = []
            for r in val or []:
                r = str(r).strip()
                if not r:
                    continue
                if not Path(r).expanduser().is_dir():
                    errors[key] = f"{r} doesn't exist."
                roots.append(pretty(Path(r).expanduser()))
            data[key] = list(dict.fromkeys(roots))
        elif key == "ignore":
            data[key] = [g.strip() for g in (val if isinstance(val, list) else str(val).splitlines()) if g.strip()]
        elif key == "threshold":
            t = float(val)
            if not 0.5 <= t <= 0.99:
                errors[key] = "Pick a value between 50% and 99%."
            data[key] = round(t, 2)
        elif key == "image_max_side":
            n = int(val)
            if not JEV_MIN_SIDE <= n <= JEV_MAX_SIDE:
                errors[key] = f"Must be between {JEV_MIN_SIDE} and {JEV_MAX_SIDE} px."
            data[key] = n
        elif key in ("send_images", "nested_projects"):
            data[key] = bool(val)
        elif key == "api_key":
            val = str(val or "").strip()
            if val:  # blank means "keep the saved key"
                data["api_key"] = val
    if updates.get("clear_api_key"):
        data.pop("api_key", None)
    if errors:
        raise SettingsError(errors)
    if data.get("dest") and Path(data["dest"]).expanduser() == Path(data.get("downloads", DEFAULTS["downloads"])).expanduser():
        raise SettingsError({"dest": "Sorted files need their own folder, not the folder being organized."})
    _write_config(data, path)
    return data


def unset_config(keys: list[str], path: Path) -> dict:
    """Remove settings, so their defaults apply again."""
    unknown = [k for k in keys if k not in DEFAULTS]
    if unknown:
        raise SorterError(f"Unknown setting: {', '.join(unknown)}. Settings: {', '.join(sorted(EDITABLE))}")
    data = read_config_file(path)
    for k in keys:
        data.pop(k, None)
    _write_config(data, path)
    return data


def _write_config(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# unorphan settings. Edited by the app's Settings panel or `unorphan config set`; safe to edit by hand.", ""]
    lines += [f"{k} = {_toml(v)}" for k, v in data.items() if v is not None]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)  # it may hold an API key


def setup_problems(cfg: dict) -> list[str]:
    """What's stopping a scan, in plain words. Empty when everything is ready."""
    out = config_problems(cfg)
    key, where = find_api_key(cfg)
    if not key:
        out.append(f"Add your Jev API key ({where}).")
    if not project_roots(cfg)[0]:
        out.append("Add at least one folder that holds your projects.")
    return out


class SettingsError(SorterError):
    """Settings that failed validation, as {setting: problem}."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


def load_folder_settings(path: Path) -> dict:
    """Custom folders, per-project hints and switched-off projects."""
    data = {"custom": [], "notes": {}, "disabled": []}
    if path.exists():
        try:
            data.update(json.loads(path.read_text()))
        except json.JSONDecodeError as e:
            raise SorterError(f"{path} is not valid JSON: {e}") from e
    return data


def save_folder_settings(data: dict, path: Path) -> dict:
    custom, seen = [], set()
    for c in data.get("custom", []):
        name = str(c.get("name", "")).strip()
        desc = str(c.get("description", "")).strip()
        if not FOLDER_NAME.match(name) or name.startswith(".") or name == NO_PROJECT:
            raise SorterError(f"'{name}' can't be used as a folder name")
        if name.lower() in seen:
            raise SorterError(f"there are two folders called '{name}'")
        if len(desc) < 10:
            raise SorterError(f"give '{name}' a description of at least a few words, so Jev knows what goes in it")
        seen.add(name.lower())
        custom.append({"name": name, "description": desc[:800]})
    clean = {
        "custom": custom,
        "notes": {str(k): str(v).strip()[:400] for k, v in data.get("notes", {}).items() if str(v).strip()},
        "disabled": sorted({str(d) for d in data.get("disabled", [])}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2))
    return clean


# --------------------------------------------------------------------------- destinations


@dataclass
class Folder:
    name: str
    paths: list[Path]  # project folders on disk; empty for custom folders
    body: str  # what the folder holds: written by you (custom) or read from the project
    note: str = ""  # your hint for a project
    custom: bool = False
    enabled: bool = True

    def text(self, limit: int = 600) -> str:
        """What Jev is told about this folder."""
        label = self.name.replace("_", " ").replace("-", " ")
        return f"{label}: " + " ".join(x for x in (self.note, self.body) if x)[:limit]


def is_project(path: Path) -> bool:
    return any((path / m).exists() for m in PROJECT_MARKERS)


def project_roots(cfg: dict) -> tuple[list[Path], bool, bool]:
    """(roots, look one level deeper?, were they auto-detected?)."""
    roots = [Path(r).expanduser() for r in cfg["project_roots"]]
    if roots:
        return roots, bool(cfg.get("nested_projects")), False
    # Nothing configured: guess common places, and look inside folders of projects (e.g. ~/dev/personal).
    return [p for p in (Path(r).expanduser() for r in AUTO_ROOTS) if p.is_dir()], True, True


def scan_root(root: Path, nested: bool, all_roots: list[Path], excluded: set[str]) -> dict[str, list[Path]]:
    """Projects directly inside root (and, if nested, inside folders that only hold projects)."""
    found: dict[str, list[Path]] = {}
    resolved = {r.resolve() for r in all_roots}

    def scan(folder: Path, depth: int) -> None:
        try:
            children = sorted(folder.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.name.startswith(".") or child.name.lower() in excluded:
                continue
            if child.resolve() in resolved:
                continue  # listed as a project folder in its own right, e.g. ~/dev/personal
            if nested and depth == 0 and not is_project(child):
                subdirs = [c for c in child.iterdir() if c.is_dir() and not c.name.startswith(".")]
                if sum(is_project(c) for c in subdirs) >= 2:
                    scan(child, depth + 1)  # a folder of projects, not a project
                    continue
            found.setdefault(child.name, []).append(child)

    if root.is_dir():
        scan(root, 0)
    return found


def find_projects(cfg: dict) -> dict[str, list[Path]]:
    roots, nested, auto = project_roots(cfg)
    if not roots:
        raise SorterError("Couldn't find your projects. Add the folders that hold them in Settings, or pass --projects-root.")
    excluded = {e.lower() for e in cfg["exclude_projects"]}
    found: dict[str, list[Path]] = {}
    for root in roots:
        for name, paths in scan_root(root, nested, roots, excluded).items():
            found.setdefault(name, []).extend(paths)
    return dict(sorted(found.items(), key=lambda kv: kv[0].lower()))


def root_report(cfg: dict) -> list[dict]:
    """For the settings panel: each project folder and what was found in it."""
    roots, nested, auto = project_roots(cfg)
    excluded = {e.lower() for e in cfg["exclude_projects"]}
    out = []
    for root in roots:
        names = sorted(scan_root(root, nested, roots, excluded), key=str.lower) if root.is_dir() else []
        with_notes = sum(1 for n in names for p in [root / n] if any((p / f).is_file() for f in ("CLAUDE.md", "README.md", "AGENTS.md")))
        out.append({"path": pretty(root), "exists": root.is_dir(), "auto": auto, "projects": names, "described": with_notes})
    return out


def all_folders(cfg: dict, settings: dict | None = None) -> list[Folder]:
    """Custom folders first, then projects. Includes switched-off ones (enabled=False)."""
    settings = settings or load_folder_settings(cfg["folders_path"])
    notes = {k.lower(): v for k, v in cfg["project_notes"].items()}
    notes.update({k.lower(): v for k, v in settings["notes"].items()})
    disabled = {d.lower() for d in settings["disabled"]}
    custom = [Folder(c["name"], [], c["description"], custom=True) for c in settings["custom"]]
    taken = {c.name.lower() for c in custom}
    projects = [
        Folder(name, paths, describe(paths), note=notes.get(name.lower(), ""), enabled=name.lower() not in disabled)
        for name, paths in find_projects(cfg).items()
        if name.lower() not in taken  # a custom folder with the same name replaces the project
    ]
    return custom + projects


def _clean_markdown(text: str) -> str:
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)  # front matter
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)  # images / badges
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links -> their text
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("|") or re.fullmatch(r"[-=*_ ]+", s):
            continue
        lines.append(re.sub(r"^#+\s*|^[>*-]\s+", "", s))
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _read_with_includes(path: Path) -> str:
    """Read a CLAUDE.md-style file, inlining `@other-file.md` include lines."""
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        m = re.fullmatch(r"\s*@([\w./-]+\.md)\s*", line)
        inc = path.parent / m.group(1) if m else None
        out.append(inc.read_text(errors="ignore") if inc and inc.is_file() else line)
    return "\n".join(out)


def describe(paths: list[Path], limit: int = 450, snippet: int = 220) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for base in paths:
        for rel in DESC_FILES:
            f = base / rel
            if not f.is_file():
                continue
            text = GENERATED_BLOCK.sub("", _read_with_includes(f))
            if any(b.search(text) for b in BOILERPLATE):
                continue
            cleaned = _clean_markdown(text)[:snippet]
            if len(cleaned) > 40 and cleaned[:80] not in seen:  # copies of one file in two places count once
                seen.add(cleaned[:80])
                parts.append(cleaned)
        for meta in ("package.json", "pyproject.toml"):
            f = base / meta
            if f.is_file():
                m = re.search(r'"?description"?\s*[:=]\s*"([^"]{10,})"', f.read_text(errors="ignore"))
                if m:
                    parts.append(m.group(1))
    if not parts:  # nothing written down: let the folder's contents speak
        entries = sorted(e.name for p in paths for e in p.iterdir() if not e.name.startswith(".") and e.name != "node_modules")
        parts.append("Contains: " + ", ".join(entries[:20]))
    return " ".join(parts)[:limit]


# --------------------------------------------------------------------------- downloads


@dataclass
class Item:
    path: Path
    kind: str
    facts: dict
    image: bytes | None = None
    result: dict = field(default_factory=dict)


def kind_of(path: Path) -> str:
    if path.is_dir():
        return "app" if path.suffix == ".app" else "folder"
    ext = path.suffix.lower()
    return next((k for k, exts in KINDS.items() if ext in exts), "file")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def list_items(cfg: dict) -> list[Path]:
    downloads, dest = cfg["downloads"], cfg["dest"]
    items = []
    for p in sorted(downloads.iterdir()):
        if p.name in SKIP_NAMES or p.name.startswith(".") or p.name.endswith(PARTIAL_SUFFIXES):
            continue
        if p.resolve() == dest.resolve() or any(fnmatch.fnmatch(p.name, g) for g in cfg["ignore"]):
            continue
        if p.is_dir() and (p / LOG_NAME).exists():
            continue  # an earlier destination folder of ours (before the setting changed)
        items.append(p)
    return items


def shrink_image(path: Path, max_side: int, quality: int, min_side: int = JEV_MIN_SIDE) -> bytes | None:
    """A small JPEG copy, padded up to min_side if needed. None if it can't be read."""
    try:
        from PIL import Image as PILImage, ImageOps

        with PILImage.open(path) as im:
            im.draft("RGB", (max_side, max_side))  # cheap JPEG downscale on decode
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = PILImage.new("RGB", im.size, "white")
                bg.paste(im, mask=im.getchannel("A"))
                im = bg
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            w, h = im.size
            if min(w, h) < min_side:  # pad tiny or very thin images up to the minimum
                if max(w, h) < min_side:
                    scale = min_side / max(w, h)
                    im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))))
                    w, h = im.size
                canvas = PILImage.new("RGB", (max(w, min_side), max(h, min_side)), "white")
                canvas.paste(im, ((canvas.width - w) // 2, (canvas.height - h) // 2))
                im = canvas
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:
        return None


def build_item(path: Path, cfg: dict) -> Item:
    kind = kind_of(path)
    st = path.stat()
    facts: dict = {"name": path.name, "kind": kind, "downloaded": dt.date.fromtimestamp(st.st_mtime).isoformat()}
    if path.is_dir():
        names = sorted(c.name for c in path.iterdir() if not c.name.startswith("."))
        facts["contains"] = names[:25] + ([f"... and {len(names) - 25} more"] if len(names) > 25 else [])
    else:
        facts["size"] = human_size(st.st_size)
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as z:
                    names = [n for n in z.namelist() if not n.startswith("__MACOSX") and "/." not in n]
                facts["contains"] = names[:25] + ([f"... and {len(names) - 25} more"] if len(names) > 25 else [])
            except (zipfile.BadZipFile, OSError):
                pass
    image = None
    if cfg["send_images"] and path.suffix.lower() in IMAGE_EXTS and path.is_file():
        image = shrink_image(path, int(cfg["image_max_side"]), int(cfg["image_quality"]))
    return Item(path, kind, facts, image)


# --------------------------------------------------------------------------- classification


def folder_criteria(folders: list[Folder]) -> dict[str, str]:
    crit = {f.name: f.text() for f in folders if f.enabled}
    crit[NO_PROJECT] = (
        "Fits none of the folders above: installers, personal documents, bank or tax papers, chat "
        "exports, random photos, or anything the name and picture don't tie to one folder."
    )
    return crit


INSTRUCTIONS = (
    "`file` is a file or folder sitting loose in someone's Downloads folder. Which of their folders "
    "should it be filed in? Folders are either one specific project or a broader topic or company. "
    "Use the file's name, what it contains, and its picture if there is one. When both a specific "
    f"project and a broader folder fit, pick the specific project. Answer {NO_PROJECT} unless one "
    "folder clearly fits."
)


def cache_key(item: Item, fingerprint: str) -> str:
    st = item.path.stat()
    raw = f"{item.path.name}|{st.st_size}|{st.st_mtime_ns}|{item.image is not None}|{fingerprint}"
    return hashlib.sha1(raw.encode()).hexdigest()


async def classify_all(
    items: list[Item],
    folders: list[Folder],
    cfg: dict,
    api_key: str,
    read_cache: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Fill in item.result for every item. Returns the input tokens used."""
    from typesafe_sdk import (
        AsyncTypeSafeClient,
        Choice,
        Image,
        TypeSafeAuthenticationError,
        TypeSafeBadRequestError,
        TypeSafePermissionDeniedError,
    )

    crit = folder_criteria(folders)
    reverse = dict(reversed(list(crit.items())))
    fingerprint = hashlib.sha1(json.dumps([crit, INSTRUCTIONS, MODEL, cfg["image_max_side"], cfg["image_quality"]]).encode()).hexdigest()
    cache: dict = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            cache = {}

    sem = asyncio.Semaphore(int(cfg["concurrency"]))
    done = tokens = 0

    async def one(client, item: Item) -> None:
        nonlocal done, tokens
        key = cache_key(item, fingerprint)
        if read_cache and key in cache:
            item.result = cache[key]
        else:
            async with sem:
                state = dict(item.facts)
                headers = None
                if item.image:
                    state["picture"] = Image(item.image)
                    headers = {"TypeSafe-Danger-Zone-Images": "true"}
                questions = {
                    "folder": Choice(instructions=INSTRUCTIONS, criteria=crit),
                    "folder_check": Choice(instructions=INSTRUCTIONS, criteria=reverse),
                }
                try:
                    try:
                        r = await client.system_one(state=state, questions=questions, extra_headers=headers)
                    except TypeSafeBadRequestError:
                        if not item.image:
                            raise
                        state.pop("picture")  # image rejected: fall back to the name alone
                        item.image = None
                        r = await client.system_one(state=state, questions=questions)
                    tokens += r.usage.input_tokens
                    a, b = r.choices["folder"], r.choices["folder_check"]
                    top3 = sorted(a.probabilities.items(), key=lambda kv: -kv[1])[:3]
                    item.result = {
                        "choice": a.choice,
                        "check": b.choice,
                        "confidence": min(a.confidence, b.confidence),
                        "top": [[k, round(v, 3)] for k, v in top3],
                        "used_image": item.image is not None,
                    }
                    cache[key] = item.result
                except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as e:
                    raise SorterError(
                        "Jev didn't accept the API key. Check it in Settings (or `unorphan config set api_key -`); "
                        "keys are at https://console.typesafe.ai/keys"
                    ) from e
                except Exception as e:  # keep going; the item just stays put
                    item.result = {"error": f"{type(e).__name__}: {e}"[:200]}
        done += 1
        if progress:
            progress(done, len(items))

    async with AsyncTypeSafeClient(api_key=api_key, model=MODEL) as client:
        await asyncio.gather(*(one(client, it) for it in items))
    if tokens:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache))
    return tokens


def decide(item: Item, threshold: float) -> tuple[str | None, str]:
    """(folder to move to or None, short reason)."""
    r = item.result
    if "error" in r:
        return None, f"error: {r['error']}"
    best, conf = r["choice"], r["confidence"]
    if best == NO_PROJECT:
        return None, "no matching folder"
    if r["check"] != best:
        return None, f"unsure: {best} vs {r['check']}"
    if conf < threshold:
        return None, f"low confidence: {best} {conf:.0%}"
    return best, f"{conf:.0%}"


# --------------------------------------------------------------------------- moving / undo


def free_target(folder: Path, name: str, is_dir: bool) -> Path:
    target = folder / name
    stem, suffix = (name, "") if is_dir else (Path(name).stem, Path(name).suffix)
    n = 2
    while target.exists():
        target = folder / f"{stem} ({n}){suffix}"
        n += 1
    return target


def apply_moves(moves: list[tuple[Path, str]], cfg: dict) -> tuple[list[str], list[str]]:
    """Move each (path, folder name) into <dest>/<folder>/. Returns (names moved, errors)."""
    dest: Path = cfg["dest"]
    run_id = dt.datetime.now().isoformat(timespec="seconds")
    dest.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    errors: list[str] = []
    with (dest / LOG_NAME).open("a") as log:
        for path, name in moves:
            if not FOLDER_NAME.match(name) or name.startswith("."):
                errors.append(f"{path.name}: bad folder name '{name}'")
                continue
            folder = dest / name
            folder.mkdir(exist_ok=True)
            target = free_target(folder, path.name, path.is_dir())
            try:
                shutil.move(str(path), str(target))
            except OSError as e:
                errors.append(f"{path.name}: {e}")
                continue
            log.write(json.dumps({"run": run_id, "from": str(path), "to": str(target)}) + "\n")
            moved.append(path.name)
    return moved, errors


def undo_last(cfg: dict) -> str:
    log = cfg["dest"] / LOG_NAME
    if not log.exists():
        raise SorterError("Nothing to undo: no moves have been logged yet.")
    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    undone = {e["undo"] for e in entries if "undo" in e}
    runs = [e["run"] for e in entries if "run" in e and e["run"] not in undone]
    if not runs:
        raise SorterError("Nothing to undo: every logged move has already been undone.")
    run = runs[-1]
    restored, problems = 0, []
    for e in reversed([e for e in entries if e.get("run") == run]):
        src, dst = Path(e["to"]), Path(e["from"])
        if not src.exists():
            problems.append(f"missing, skipped: {src}")
        elif dst.exists():
            problems.append(f"something is already at {dst}, left {src.name} where it is")
        else:
            shutil.move(str(src), str(dst))
            restored += 1
    for folder in {Path(e["to"]).parent for e in entries if e.get("run") == run}:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    with log.open("a") as f:
        f.write(json.dumps({"undo": run}) + "\n")
    return "\n".join([f"Put back {restored} item(s) from the move at {run}."] + problems)
