# fastflowtransform/cli/docs_cmd.py
from __future__ import annotations

import contextlib
import queue
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import typer

from fastflowtransform.cli.bootstrap import _prepare_context
from fastflowtransform.cli.docs_utils import _resolve_dag_out_dir
from fastflowtransform.cli.options import (
    EngineOpt,
    EnvOpt,
    OutOpt,
    ProjectArg,
    VarsOpt,
    WithSchemaOpt,
)
from fastflowtransform.core import REGISTRY
from fastflowtransform.docs import render_site
from fastflowtransform.logging import echo, echo_debug


# ---------------------------
# Hot-reload broadcaster (SSE)
# ---------------------------
@dataclass
class _SSEClient:
    q: queue.Queue[str] = field(default_factory=queue.Queue)


class _ReloadHub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[_SSEClient] = []

    def add(self) -> _SSEClient:
        c = _SSEClient()
        with self._lock:
            self._clients.append(c)
        return c

    def remove(self, c: _SSEClient) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._clients.remove(c)

    def broadcast_reload(self) -> None:
        with self._lock:
            for c in list(self._clients):
                # SSE "reload" event
                c.q.put("event: reload\ndata: 1\n\n")

    def broadcast_log(self, msg: str) -> None:
        # Optional: can be used later for in-browser toast/logging
        payload = msg.replace("\n", " ").strip()
        with self._lock:
            for c in list(self._clients):
                c.q.put(f"event: log\ndata: {payload}\n\n")


# ---------------------------
# HTTP handler
# ---------------------------
_RELOAD_JS = r"""
(() => {
  const es = new EventSource("/__fft_events");
  es.addEventListener("reload", () => {
    // Hard reload to also refresh CSS/JS
    window.location.reload();
  });
  // Optional future: show server logs in UI
  es.addEventListener("log", (ev) => {
    // console.log("[FFT]", ev.data);
  });
  es.onerror = () => {
    // auto-reconnect is handled by EventSource, but we can hint in console
    // console.warn("[FFT] reload channel error; reconnecting…");
  };
})();
""".lstrip()


class _DocsHandler(SimpleHTTPRequestHandler):
    """
    - Serves static files from a directory
    - SPA fallback: missing paths -> index.html (except /assets/* and /__fft_*)
    - SSE endpoint for hot reload
    - HTML injection to load reload script (dev only)
    """

    server_version = "FFTDocs/1.0"

    def __init__(
        self,
        *args: Any,
        directory: str | None = None,
        hub: _ReloadHub | None = None,
        inject_reload: bool = False,
        **kwargs: Any,
    ) -> None:
        self._hub = hub
        self._inject_reload = inject_reload
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        # Make dev-server behavior more predictable
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        # Special endpoints first
        if self.path == "/__fft_reload.js":
            self._serve_text(_RELOAD_JS, content_type="application/javascript; charset=utf-8")
            return

        if self.path == "/__fft_events":
            self._serve_sse()
            return

        # Try regular file first
        local_path = self.translate_path(self.path)
        p = Path(local_path)

        # SPA fallback (don't eat assets or internal endpoints)
        req_path = unquote(self.path.split("?", 1)[0])
        if (
            not p.exists()
            and not req_path.startswith("/assets/")
            and not req_path.startswith("/__fft_")
        ):
            idx = Path(self.translate_path("/index.html"))
            if idx.exists():
                self._serve_file(idx, inject=self._inject_reload)
                return

        # Serve real file, with optional HTML injection
        if p.exists() and p.is_file() and p.suffix.lower() in (".html", ".htm"):
            self._serve_file(p, inject=self._inject_reload)
            return

        # Default static behavior
        super().do_GET()

    def _serve_text(self, text: str, *, content_type: str) -> None:
        data = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, p: Path, *, inject: bool) -> None:
        data = p.read_bytes()
        ctype = self.guess_type(str(p))

        if inject and ctype.startswith("text/html"):
            try:
                s = data.decode("utf-8")
                tag = '<script src="/__fft_reload.js"></script>\n'
                s = s.replace("</body>", f"{tag}</body>") if "</body>" in s else s + "\n" + tag
                data = s.encode("utf-8")
            except Exception:
                # If decoding fails, serve raw.
                pass

        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") else ctype
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_sse(self) -> None:
        if not self._hub:
            self.send_error(HTTPStatus.NOT_FOUND, "Reload hub not configured")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        client = self._hub.add()
        try:
            # Initial hello + faster reconnect
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()

            while True:
                try:
                    msg = client.q.get(timeout=15.0)
                except queue.Empty:
                    # keep-alive
                    msg = "event: ping\ndata: 1\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            # best-effort cleanup
            with contextlib.suppress(Exception):
                self._hub.remove(client)


# ---------------------------
# Watcher
# ---------------------------
def _glob_many(root: Path, patterns: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        out.extend(root.glob(pat))
    return [p for p in out if p.exists()]


def _collect_watch_paths(project_dir: Path) -> list[Path]:
    # Project files affecting docs content
    paths: list[Path] = []
    paths += [project_dir / "project.yml"]
    paths += [project_dir / "sources.yml"]
    paths += _glob_many(project_dir, ["docs/**/*.md", "docs/**/*.yml", "docs/**/*.yaml"])
    paths += _glob_many(project_dir, ["models/**/*.ff.sql", "models/**/*.ff.py"])
    paths += _glob_many(project_dir, ["macros/**/*.sql", "macros/**/*.py"])

    # Bundled templates + assets (docs.py loads templates from
    # package/templates) :contentReference[oaicite:3]{index=3}
    pkg_templates = Path(__file__).resolve().parents[1] / "templates"
    if pkg_templates.exists():
        paths += _glob_many(pkg_templates, ["**/*.j2", "assets/**/*"])

    # Keep only files
    return sorted({p.resolve() for p in paths if p.exists() and p.is_file()})


def _snapshot_mtime(paths: list[Path]) -> dict[Path, int]:
    snap: dict[Path, int] = {}
    for p in paths:
        try:
            snap[p] = p.stat().st_mtime_ns
        except FileNotFoundError:
            continue
    return snap


def _watch_loop(
    *,
    project_dir: Path,
    poll_s: float,
    debounce_s: float,
    on_change: Callable,
) -> None:
    watched = _collect_watch_paths(project_dir)
    prev = _snapshot_mtime(watched)
    last_trigger = 0.0

    echo_debug(f"Watching {len(watched)} files for docs reload")

    while True:
        time.sleep(poll_s)

        # Refresh list occasionally (new files added)
        if int(time.time()) % 10 == 0:
            watched = _collect_watch_paths(project_dir)

        cur = _snapshot_mtime(watched)

        changed: list[Path] = []
        for p, mt in cur.items():
            if prev.get(p) != mt:
                changed.append(p)

        # Removed files
        for p in list(prev.keys()):
            if p not in cur:
                changed.append(p)

        if changed:
            now = time.time()
            if now - last_trigger < debounce_s:
                prev = cur
                continue
            last_trigger = now
            prev = cur

            # Pick a representative file for log
            head = changed[0]
            echo_debug(f"Docs change detected: {head}")
            with contextlib.suppress(Exception):
                on_change(head)


# ---------------------------
# CLI command
# ---------------------------
docs_app = typer.Typer(help="Docs tooling (dev server, live docs)")


def _build_docs_once(
    *,
    project: str,
    env_name: str,
    engine: Any,
    vars: list[str] | None,
    out: Path | None,
    with_schema: bool,
) -> tuple[Path, Any]:
    if out is not None:
        out = out.resolve()
        out.mkdir(parents=True, exist_ok=True)

    ctx = _prepare_context(project, env_name, engine, vars)
    ex, *_ = ctx.make_executor()

    out_dir = _resolve_dag_out_dir(ctx.project, out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    render_site(out_dir, REGISTRY.nodes, executor=ex, with_schema=with_schema)
    dt = time.time() - t0

    echo(f"Docs written to {out_dir / 'index.html'} ({dt:.2f}s)")
    return out_dir, ctx


@docs_app.command("serve", help="Serve docs with live reload (watch templates/YAML/MD/schema).")
def serve(
    project: ProjectArg = ".",
    env_name: EnvOpt = "dev",
    engine: EngineOpt = None,
    vars: VarsOpt = None,
    out: OutOpt = None,
    with_schema: WithSchemaOpt = True,
    port: int = typer.Option(8000, "--port", help="Port to bind (default: 8000)"),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind (default: 127.0.0.1)"),
    watch: bool = typer.Option(
        True,
        "--watch/--no-watch",
        help="Watch project + templates and hot reload.",
        show_default=True,
    ),
    open_browser: bool = typer.Option(False, "--open", help="Open docs in the default browser."),
    poll: float = typer.Option(0.5, "--poll", help="Watch poll interval (seconds)."),
    debounce: float = typer.Option(0.35, "--debounce", help="Debounce rebuilds (seconds)."),
) -> None:
    # Initial build
    out_dir, _ = _build_docs_once(
        project=project,
        env_name=env_name,
        engine=engine,
        vars=vars,
        out=out,
        with_schema=with_schema,
    )

    url = f"http://{host}:{port}/"
    echo(f"Serving docs from {out_dir} at {url}")

    hub = _ReloadHub()

    # Rebuild callback used by watcher
    rebuild_lock = threading.Lock()

    def _rebuild(changed: Path) -> None:
        with rebuild_lock:
            echo_debug(f"Rebuilding docs due to: {changed}")
            try:
                _build_docs_once(
                    project=project,
                    env_name=env_name,
                    engine=engine,
                    vars=vars,
                    out=out,
                    with_schema=with_schema,
                )
                hub.broadcast_reload()
            except Exception as e:
                # Keep server alive
                echo(f"Docs rebuild failed: {e}")

    if watch:
        t = threading.Thread(
            target=_watch_loop,
            kwargs=dict(
                project_dir=Path(project).resolve(),
                poll_s=poll,
                debounce_s=debounce,
                on_change=_rebuild,
            ),
            daemon=True,
        )
        t.start()

    handler = partial(_DocsHandler, directory=str(out_dir), hub=hub, inject_reload=True)
    httpd = ThreadingHTTPServer((host, port), handler)

    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url, new=2)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        echo("Stopping docs server…")
    finally:
        httpd.server_close()


def register(app: typer.Typer) -> None:
    app.add_typer(docs_app, name="docs")


__all__ = ["register"]
