"""A small local web UI: settings, folders, Jev's suggestions, move, undo.

Listens on 127.0.0.1 only. Every API call must carry a random token that is
baked into the page at startup, so other websites open in the same browser
can't drive it.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import core
from .core import IMAGE_EXTS, JEV_MAX_SIDE, JEV_MIN_SIDE, PRICE_PER_MTOK, SettingsError, SorterError, pretty


def native_folder_picker() -> str | None:
    """Which OS folder picker we can open, if any."""
    if sys.platform == "darwin" and shutil.which("osascript"):
        return "macos"
    if sys.platform.startswith("linux") and os.environ.get("DISPLAY") and shutil.which("zenity"):
        return "zenity"
    return None


def choose_folder(prompt: str, start: str | None) -> str | None:
    """Open the OS folder picker. Returns the chosen path, or None if cancelled."""
    picker = native_folder_picker()
    start_dir = Path(start).expanduser() if start else Path.home()
    if not start_dir.is_dir():
        start_dir = Path.home()
    if picker == "macos":
        script = (
            f"POSIX path of (choose folder with prompt {json.dumps(prompt)} "
            f"default location (POSIX file {json.dumps(str(start_dir))}))"
        )
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    elif picker == "zenity":
        r = subprocess.run(["zenity", "--file-selection", "--directory", f"--title={prompt}", f"--filename={start_dir}/"],
                           capture_output=True, text=True, timeout=600)
    else:
        return None
    path = r.stdout.strip()
    return pretty(Path(path.rstrip("/") or "/")) if r.returncode == 0 and path else None


class App:
    def __init__(self, config_path: Path | None, overrides: dict):
        self.config_path = config_path
        self.overrides = overrides  # command-line flags; dropped once settings are saved from the UI
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.scan = {"status": "idle", "done": 0, "total": 0, "tokens": 0, "items": [], "error": None}
        self.thumbs: dict[tuple[str, int], bytes] = {}
        self.reload()

    def reload(self) -> None:
        self.cfg = core.load_config(self.config_path, self.overrides, validate=False)
        self.thumbs.clear()

    # ----------------------------------------------------------------- state

    def problems(self) -> list[str]:
        out = core.config_problems(self.cfg)
        key, where = core.find_api_key(self.cfg)
        if not key:
            out.append(f"Add your Jev API key ({where}).")
        if not core.project_roots(self.cfg)[0]:
            out.append("Add at least one folder that holds your projects.")
        return out

    def folders_payload(self) -> list[dict]:
        try:
            folders = core.all_folders(self.cfg)
        except SorterError:
            return []
        return [{
            "name": f.name,
            "custom": f.custom,
            "enabled": f.enabled,
            "description": f.body if f.custom else "",
            "auto": "" if f.custom else f.body,
            "hint": f.note,
            "paths": [pretty(p) for p in f.paths],
        } for f in folders]

    def settings_payload(self) -> dict:
        saved = core.read_config_file(self.config_path)
        key, where = core.find_api_key(self.cfg)
        return {
            "downloads": pretty(self.cfg["downloads"]),
            "dest": saved.get("dest", ""),
            "dest_resolved": pretty(self.cfg["dest"]),
            "project_roots": [pretty(Path(r).expanduser()) for r in self.cfg["project_roots"]],
            "roots": core.root_report(self.cfg),
            "nested_projects": core.project_roots(self.cfg)[1],  # auto-detect always looks one level in
            "ignore": list(self.cfg["ignore"]),
            "threshold": float(self.cfg["threshold"]),
            "send_images": bool(self.cfg["send_images"]),
            "image_max_side": int(self.cfg["image_max_side"]),
            "env_file": pretty(Path(self.cfg["env_file"]).expanduser()) if self.cfg.get("env_file") else "",
            "key_found": bool(key),
            "key_source": where,
            "key_saved": bool(saved.get("api_key")),
            "image_limits": [JEV_MIN_SIDE, JEV_MAX_SIDE],
            "picker": native_folder_picker(),
            "config_path": pretty(self.config_path or core.CONFIG_PATH),
            "folders_path": pretty(core.FOLDERS_PATH),
        }

    def state(self) -> dict:
        with self.lock:
            scan = dict(self.scan)
        return {
            "downloads": pretty(self.cfg["downloads"]),
            "dest": pretty(self.cfg["dest"]),
            "threshold": float(self.cfg["threshold"]),
            "price_per_mtok": PRICE_PER_MTOK,
            "problems": self.problems(),
            "folders": self.folders_payload(),
            "scan": scan,
        }

    # ----------------------------------------------------------------- scan

    def start_scan(self, fresh: bool) -> None:
        problems = self.problems()
        with self.lock:
            if self.scan["status"] == "running":
                return
            if problems:
                self.scan.update(status="setup", items=[], error=None)
                return
            self.scan.update(status="running", done=0, total=0, tokens=0, error=None)
        threading.Thread(target=self._scan, args=(fresh,), daemon=True).start()

    def _scan(self, fresh: bool) -> None:
        try:
            cfg = self.cfg
            folders = core.all_folders(cfg)
            paths = core.list_items(cfg)
            with self.lock:
                self.scan["total"] = len(paths)
            api_key = core.resolve_api_key(cfg)
            items = [core.build_item(p, cfg) for p in paths]

            def progress(done: int, total: int) -> None:
                with self.lock:
                    self.scan["done"] = done

            tokens = asyncio.run(core.classify_all(items, folders, cfg, api_key, read_cache=not fresh, progress=progress))
            rows = [self._row(it) for it in items]
            with self.lock:
                self.scan.update(status="done", items=rows, tokens=tokens)
        except Exception as e:  # shown in the UI
            with self.lock:
                self.scan.update(status="error", error=str(e))

    def _row(self, it: core.Item) -> dict:
        r = it.result
        return {
            "name": it.path.name,
            "kind": it.kind,
            "size": it.facts.get("size", ""),
            "date": it.facts["downloaded"],
            "image": it.path.suffix.lower() in IMAGE_EXTS and it.path.is_file(),
            "choice": r.get("choice"),
            "check": r.get("check"),
            "confidence": r.get("confidence", 0),
            "top": r.get("top", []),
            "used_image": r.get("used_image", False),
            "error": r.get("error"),
        }

    # ----------------------------------------------------------------- actions

    def save_settings(self, body: dict) -> dict:
        core.save_config(body, self.config_path)
        self.overrides = {}
        self.reload()
        self.start_scan(fresh=False)
        return {"ok": True, "settings": self.settings_payload()}

    def check_path(self, path: str, kind: str) -> dict:
        p = Path(path.strip()).expanduser() if path.strip() else None
        if not p:
            return {"ok": False, "message": "Enter a folder."}
        if not p.is_dir():
            return {"ok": False, "message": "That folder doesn't exist."}
        if kind == "root":
            roots = [Path(r).expanduser() for r in self.cfg["project_roots"]] + [p]
            names = sorted(core.scan_root(p, bool(self.cfg.get("nested_projects")), roots, set()), key=str.lower)
            return {"ok": True, "path": pretty(p), "projects": names,
                    "message": f"{len(names)} project folder{'s' if len(names) != 1 else ''} found" if names else "No project folders found inside it."}
        loose = [c for c in p.iterdir() if not c.name.startswith(".")]
        return {"ok": True, "path": pretty(p), "message": f"{len(loose)} item{'s' if len(loose) != 1 else ''} in it"}

    def current_items(self) -> dict[str, Path]:
        return {p.name: p for p in core.list_items(self.cfg)}

    def move(self, moves: list[dict]) -> dict:
        loose = self.current_items()
        known = {f.name for f in core.all_folders(self.cfg)}
        todo, errors = [], []
        for m in moves:
            name, folder = str(m.get("name", "")), str(m.get("folder", ""))
            if name not in loose:
                errors.append(f"{name}: no longer there")
            elif folder not in known:
                errors.append(f"{name}: unknown folder '{folder}'")
            else:
                todo.append((loose[name], folder))
        moved, move_errors = core.apply_moves(todo, self.cfg)
        done = set(moved)
        with self.lock:
            self.scan["items"] = [r for r in self.scan["items"] if r["name"] not in done]
        return {"moved": len(moved), "moved_names": moved, "errors": errors + move_errors}

    def thumb(self, name: str, size: int) -> bytes | None:
        key = (name, size)
        if key not in self.thumbs:
            path = self.current_items().get(name)
            if not path or path.suffix.lower() not in IMAGE_EXTS:
                return None
            data = core.shrink_image(path, size, 70, min_side=1)
            if data is None:
                return None
            self.thumbs[key] = data
        return self.thumbs[key]


def make_handler(app: App, page: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "unorphan"

        def log_message(self, *args) -> None:  # keep the terminal quiet
            pass

        # -- helpers
        def _host_ok(self) -> bool:  # blocks DNS-rebinding tricks
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _authorized(self, query: dict | None = None) -> bool:
            sent = self.headers.get("X-Token") or (query or {}).get("t", [""])[0]
            return self._host_ok() and secrets.compare_digest(sent, app.token)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n < 2_000_000 else {}

        # -- routes
        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
            if url.path == "/":
                csp = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"
                body = page.replace("__TOKEN__", app.token).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", csp)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not self._authorized(q):
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            try:
                if url.path == "/api/state":
                    return self._json(app.state())
                if url.path == "/api/settings":
                    return self._json(app.settings_payload())
                if url.path == "/api/progress":
                    with app.lock:
                        s = {k: app.scan[k] for k in ("status", "done", "total", "tokens", "error")}
                    return self._json(s)
                if url.path == "/api/thumb":
                    size = 96 if q.get("s", ["s"])[0] == "s" else 640
                    data = app.thumb(q.get("name", [""])[0], size)
                    if data is None:
                        return self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                    return self._send(200, data, "image/jpeg")
            except SorterError as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

        def do_POST(self) -> None:
            if not self._authorized():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/folders":
                    core.save_folder_settings(body)
                    return self._json({"ok": True, "folders": app.folders_payload()})
                if path == "/api/settings":
                    return self._json(app.save_settings(body))
                if path == "/api/check-path":
                    return self._json(app.check_path(str(body.get("path", "")), str(body.get("kind", ""))))
                if path == "/api/choose-folder":
                    chosen = choose_folder(str(body.get("prompt") or "Choose a folder"), body.get("start"))
                    return self._json({"path": chosen})
                if path == "/api/scan":
                    app.start_scan(fresh=bool(body.get("fresh")))
                    return self._json({"ok": True})
                if path == "/api/move":
                    return self._json(app.move(body.get("moves", [])))
                if path == "/api/undo":
                    return self._json({"message": core.undo_last(app.cfg)})
            except SettingsError as e:
                return self._json({"error": str(e), "fields": e.errors}, HTTPStatus.BAD_REQUEST)
            except SorterError as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._json({"error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(config_path: Path | None, overrides: dict, port: int = 0, open_browser: bool = True) -> None:
    page = resources.files(__package__).joinpath("ui.html").read_text(encoding="utf-8")
    app = App(config_path, overrides)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, page))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Unorphan is running at {url}\nPress Ctrl+C to stop.", flush=True)
    problems = app.problems()
    if problems:
        print("Setup needed (the Settings panel will open): " + " ".join(problems), flush=True)
    app.start_scan(fresh=False)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
