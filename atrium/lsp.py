"""Language servers behind the Files editor: semantic highlighting, go to definition,
hover, references, completion and diagnostics.

One server process per (language, workspace root), shared by every browser tab.
The browser speaks LSP over ``/ws/lsp``; Atrium owns the server side of the
conversation: it initializes the server, answers the server's own requests
(configuration, progress, registrations), reference-counts open documents across
tabs (rewriting versions) and routes responses back to the tab that asked.

Files outside the project that the server points at (stdlib, site-packages,
bundled stubs) become readable for the tab that received the location, so
⌘-click into a library opens it read-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .config import config

log = logging.getLogger("atrium.lsp")

IDLE_SHUTDOWN_SECONDS = 15 * 60
MAX_READ_BYTES = 4_000_000
_LOCATION_METHODS = {
    "textDocument/definition", "textDocument/declaration", "textDocument/typeDefinition",
    "textDocument/implementation", "textDocument/references",
}


def uri_to_path(uri: str) -> str:
    return unquote(urlparse(uri).path)


def path_to_uri(path: str) -> str:
    return Path(path).as_uri()


# ---------------------------------------------------------------------------
# server registry
# ---------------------------------------------------------------------------
def _python_interpreter(root: str, project_dir: str) -> str:
    for base in dict.fromkeys([root, project_dir]):
        for venv in (".venv", "venv", ".env", "env"):
            py = Path(base) / venv / "bin" / "python"
            if py.exists():
                return str(py)
    return shutil.which("python3") or sys.executable


def _python_settings(section: str, root: str, project_dir: str) -> Any:
    analysis = {
        "typeCheckingMode": config.get("lsp.python.typeCheckingMode") or "standard",
        "diagnosticMode": "openFilesOnly",
        "autoSearchPaths": True,
        "useLibraryCodeForTypes": True,
        "autoImportCompletions": True,
    }
    python = {"pythonPath": _python_interpreter(root, project_dir), "analysis": analysis}
    return {
        "python": python,
        "python.analysis": analysis,
        "basedpyright": {"analysis": analysis},
        "basedpyright.analysis": analysis,
        "pyright": {},
    }.get(section)


@dataclass(frozen=True)
class ServerSpec:
    language: str  # Monaco language id
    candidates: tuple[tuple[str, ...], ...]  # argv candidates, first found wins
    settings: Callable[[str, str, str], Any] = lambda _section, _root, _project: None
    init_options: dict[str, Any] = field(default_factory=dict)

    def argv(self) -> list[str] | None:
        override = (config.get("lsp.commands") or {}).get(self.language)
        if override:
            argv = [override] if isinstance(override, str) else list(override)
            exe = _find(argv[0])
            return [exe, *argv[1:]] if exe else None
        for cand in self.candidates:
            exe = _find(cand[0])
            if exe:
                return [exe, *cand[1:]]
        return None


def _find(name: str) -> str | None:
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) else None
    local = Path(sys.executable).parent / name  # Atrium's own venv (basedpyright is a dependency)
    if local.exists():
        return str(local)
    return shutil.which(name)


SERVERS: dict[str, ServerSpec] = {
    "python": ServerSpec(
        "python",
        (("basedpyright-langserver", "--stdio"), ("pyright-langserver", "--stdio"), ("pylsp",)),
        settings=_python_settings,
    ),
    "c": ServerSpec("c", (("clangd",),)),
    "cpp": ServerSpec("cpp", (("clangd",),)),
    "go": ServerSpec("go", (("gopls",),)),
    "rust": ServerSpec("rust", (("rust-analyzer",),)),
}


def available() -> dict[str, dict[str, Any]]:
    """Languages with a server on this machine (for /api/meta)."""
    if not config.get("lsp.enabled"):
        return {}
    out = {}
    for lang, spec in SERVERS.items():
        argv = spec.argv()
        if argv:
            out[lang] = {"server": Path(argv[0]).name}
    return out


CLIENT_CAPABILITIES: dict[str, Any] = {
    "general": {"positionEncodings": ["utf-16"]},
    "workspace": {
        "configuration": True,
        "workspaceFolders": True,
        "didChangeConfiguration": {"dynamicRegistration": True},
        "semanticTokens": {"refreshSupport": True},
    },
    "window": {"workDoneProgress": True},
    "textDocument": {
        "synchronization": {"didSave": False, "dynamicRegistration": False},
        "hover": {"contentFormat": ["markdown", "plaintext"]},
        "definition": {"linkSupport": True},
        "declaration": {"linkSupport": True},
        "typeDefinition": {"linkSupport": True},
        "implementation": {"linkSupport": True},
        "references": {},
        "documentHighlight": {},
        "signatureHelp": {
            "signatureInformation": {
                "documentationFormat": ["markdown", "plaintext"],
                "parameterInformation": {"labelOffsetSupport": True},
                "activeParameterSupport": True,
            },
            "contextSupport": True,
        },
        "completion": {
            "completionItem": {
                "snippetSupport": True,
                "documentationFormat": ["markdown", "plaintext"],
                "labelDetailsSupport": True,
                "insertReplaceSupport": True,
                "resolveSupport": {"properties": ["documentation", "detail", "additionalTextEdits"]},
                "tagSupport": {"valueSet": [1]},
            },
            "contextSupport": True,
        },
        "publishDiagnostics": {"relatedInformation": True, "tagSupport": {"valueSet": [1, 2]}},
        "semanticTokens": {
            "requests": {"full": True, "range": False},
            "tokenTypes": [
                "namespace", "type", "class", "enum", "interface", "struct", "typeParameter", "parameter",
                "variable", "property", "enumMember", "event", "function", "method", "macro", "keyword",
                "modifier", "comment", "string", "number", "regexp", "operator", "decorator",
            ],
            "tokenModifiers": [
                "declaration", "definition", "readonly", "static", "deprecated", "abstract", "async",
                "modification", "documentation", "defaultLibrary",
            ],
            "formats": ["relative"],
            "multilineTokenSupport": False,
            "overlappingTokenSupport": False,
        },
    },
}


# ---------------------------------------------------------------------------
# one server process
# ---------------------------------------------------------------------------
class LspServer:
    def __init__(self, spec: ServerSpec, argv: list[str], root: str, project_dir: str):
        self.spec = spec
        self.argv = argv
        self.root = root
        self.project_dir = project_dir
        self.key = f"{spec.language}:{root}"
        self.proc: asyncio.subprocess.Process | None = None
        self.capabilities: dict[str, Any] = {}
        self.ready = asyncio.Event()
        self.failed: str | None = None
        self.clients: set["LspClient"] = set()
        self.docs: dict[str, dict[str, Any]] = {}  # uri -> {"version": int, "owners": set[client]}
        self._next_id = 0
        self._pending: dict[int, tuple["LspClient" | None, Any, str, asyncio.Future | None]] = {}
        self._by_client: dict[tuple[int, Any], int] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self.last_used = time.time()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    # ---- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, cwd=self.root, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024,
            )
        except OSError as e:
            self.failed = f"could not start {self.argv[0]}: {e}"
            self.ready.set()
            return
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._drain_stderr())]
        root_uri = path_to_uri(self.root)
        try:
            result = await asyncio.wait_for(self._request("initialize", {
                "processId": os.getpid(),
                "clientInfo": {"name": "Atrium"},
                "rootUri": root_uri,
                "rootPath": self.root,
                "workspaceFolders": [{"uri": root_uri, "name": Path(self.root).name}],
                "capabilities": CLIENT_CAPABILITIES,
                "initializationOptions": self.spec.init_options,
            }), timeout=60)
            self.capabilities = result.get("capabilities") or {}
            await self._write({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        except Exception as e:  # noqa: BLE001 — any startup failure disables the server
            self.failed = f"{Path(self.argv[0]).name} failed to start: {e}"
            log.warning("%s: %s", self.key, self.failed)
            await self.stop()
        self.ready.set()

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            try:
                await asyncio.wait_for(self._raw_write(proc, {"jsonrpc": "2.0", "id": -1, "method": "shutdown"}), 2)
                await asyncio.wait_for(self._raw_write(proc, {"jsonrpc": "2.0", "method": "exit"}), 2)
                await asyncio.wait_for(proc.wait(), 3)
            except Exception:  # noqa: BLE001
                pass
            if proc.returncode is None:
                proc.kill()
        for t in self._tasks:
            t.cancel()
        for _client, _cid, _method, fut in self._pending.values():
            if fut and not fut.done():
                fut.set_exception(ConnectionError("server stopped"))
        self._pending.clear()

    # ---- wire ---------------------------------------------------------------
    @staticmethod
    async def _raw_write(proc: asyncio.subprocess.Process, msg: dict[str, Any]) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode()
        assert proc.stdin is not None
        proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        await proc.stdin.drain()

    async def _write(self, msg: dict[str, Any]) -> None:
        if not self.alive:
            raise ConnectionError("language server is not running")
        async with self._write_lock:
            await self._raw_write(self.proc, msg)  # type: ignore[arg-type]

    async def _request(self, method: str, params: Any) -> Any:
        self._next_id += 1
        sid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[sid] = (None, None, method, fut)
        await self._write({"jsonrpc": "2.0", "id": sid, "method": method, "params": params})
        return await fut

    async def _read_loop(self) -> None:
        proc = self.proc
        assert proc and proc.stdout
        try:
            while True:
                length = 0
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    name, _, value = line.decode("ascii", "replace").partition(":")
                    if name.lower() == "content-length":
                        length = int(value.strip())
                body = await proc.stdout.readexactly(length)
                try:
                    msg = json.loads(body)
                except ValueError:
                    continue
                await self._dispatch(msg)
        except (EOFError, asyncio.IncompleteReadError, ConnectionError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: reader crashed", self.key)
        if self.proc is proc:
            self.proc = None
            log.info("%s exited (%s)", self.key, proc.returncode)
            for c in list(self.clients):
                c.post({"op": "exit", "key": self.key})
            registry.forget(self)

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            log.debug("%s stderr: %s", self.key, line.decode(errors="replace").rstrip())

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:
            await self._answer_server_request(msg)
        elif "method" in msg:
            self._on_notification(msg)
        elif "id" in msg:
            entry = self._pending.pop(msg["id"], None)
            if not entry:
                return
            client, cid, method, fut = entry
            if fut is not None:
                if "error" in msg:
                    fut.set_exception(RuntimeError(msg["error"].get("message")))
                else:
                    fut.set_result(msg.get("result"))
                return
            assert client is not None
            self._by_client.pop((id(client), cid), None)
            if method in _LOCATION_METHODS and msg.get("result"):
                client.allow_locations(msg["result"])
            client.post({"op": "rpc", "key": self.key, "msg": {**msg, "id": cid}})

    async def _answer_server_request(self, msg: dict[str, Any]) -> None:
        method, params = msg["method"], msg.get("params") or {}
        result: Any = None
        error = None
        if method == "workspace/configuration":
            result = [self.spec.settings(item.get("section") or "", self.root, self.project_dir) for item in params.get("items", [])]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": path_to_uri(self.root), "name": Path(self.root).name}]
        elif method == "workspace/semanticTokens/refresh":
            for c in list(self.clients):
                c.post({"op": "refresh", "key": self.key})
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "not supported by Atrium"}
        elif method in ("window/workDoneProgress/create", "client/registerCapability", "client/unregisterCapability",
                        "window/showMessageRequest", "workspace/diagnostic/refresh", "workspace/inlayHint/refresh",
                        "workspace/codeLens/refresh"):
            result = None
        else:
            error = {"code": -32601, "message": f"{method} is not supported"}
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
        if error:
            reply["error"] = error
        else:
            reply["result"] = result
        try:
            await self._write(reply)
        except ConnectionError:
            pass

    def _on_notification(self, msg: dict[str, Any]) -> None:
        method = msg["method"]
        if method == "textDocument/publishDiagnostics":
            uri = (msg.get("params") or {}).get("uri")
            doc = self.docs.get(uri)
            targets = doc["owners"] if doc else self.clients
            for c in list(targets):
                c.post({"op": "rpc", "key": self.key, "msg": msg})
        elif method == "window/showMessage":
            log.info("%s: %s", self.key, (msg.get("params") or {}).get("message"))

    # ---- from browser tabs ----------------------------------------------------
    async def from_client(self, client: "LspClient", msg: dict[str, Any]) -> None:
        self.last_used = time.time()
        await self.ready.wait()
        if not self.alive:
            if "id" in msg and "method" in msg:
                client.post({"op": "rpc", "key": self.key, "msg": {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32803, "message": "language server is not running"}}})
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        if method in ("textDocument/didOpen", "textDocument/didChange", "textDocument/didClose"):
            await self._sync(client, method, params)
        elif method == "$/cancelRequest":
            sid = self._by_client.get((id(client), params.get("id")))
            if sid is not None:
                await self._write({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": sid}})
        elif "id" in msg and method:
            self._next_id += 1
            sid = self._next_id
            self._pending[sid] = (client, msg["id"], method, None)
            self._by_client[(id(client), msg["id"])] = sid
            await self._write({"jsonrpc": "2.0", "id": sid, "method": method, "params": params})
        elif method:
            await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _sync(self, client: "LspClient", method: str, params: dict[str, Any]) -> None:
        """Documents are shared between tabs: the first open opens, later opens and every
        change become full-text changes with server-side versions, the last close closes."""
        td = params.get("textDocument") or {}
        uri = td.get("uri")
        if not uri:
            return
        doc = self.docs.get(uri)
        if method == "textDocument/didOpen":
            if doc is None:
                self.docs[uri] = {"version": 1, "owners": {client}}
                await self._write({"jsonrpc": "2.0", "method": method, "params": {"textDocument": {**td, "version": 1}}})
            else:
                doc["owners"].add(client)
                doc["version"] += 1
                await self._write({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {
                    "textDocument": {"uri": uri, "version": doc["version"]}, "contentChanges": [{"text": td.get("text", "")}]}})
        elif method == "textDocument/didChange":
            if doc is None or client not in doc["owners"]:
                return
            doc["version"] += 1
            await self._write({"jsonrpc": "2.0", "method": method, "params": {
                "textDocument": {"uri": uri, "version": doc["version"]}, "contentChanges": params.get("contentChanges") or []}})
        else:
            if doc is None:
                return
            doc["owners"].discard(client)
            if not doc["owners"]:
                del self.docs[uri]
                await self._write({"jsonrpc": "2.0", "method": method, "params": {"textDocument": {"uri": uri}}})

    async def detach(self, client: "LspClient") -> None:
        self.clients.discard(client)
        for (cid_owner, _cid), sid in list(self._by_client.items()):
            if cid_owner == id(client):
                self._by_client.pop((cid_owner, _cid), None)
                self._pending.pop(sid, None)
        for uri in [u for u, d in self.docs.items() if client in d["owners"]]:
            try:
                await self._sync(client, "textDocument/didClose", {"textDocument": {"uri": uri}})
            except ConnectionError:
                pass
        self.last_used = time.time()


class LspRegistry:
    def __init__(self) -> None:
        self._servers: dict[str, LspServer] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    async def get(self, language: str, root: str, project_dir: str) -> LspServer | None:
        spec = SERVERS.get(language)
        if not spec or not config.get("lsp.enabled"):
            return None
        key = f"{language}:{root}"
        async with self._lock:
            srv = self._servers.get(key)
            if srv and (srv.alive or not srv.ready.is_set()):
                return srv
            argv = spec.argv()
            if not argv:
                return None
            srv = LspServer(spec, argv, root, project_dir)
            self._servers[key] = srv
            asyncio.create_task(srv.start())
            if not self._reaper:
                self._reaper = asyncio.create_task(self._reap())
            return srv

    def forget(self, srv: LspServer) -> None:
        if self._servers.get(srv.key) is srv:
            del self._servers[srv.key]

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(60)
            for srv in list(self._servers.values()):
                if not srv.clients and time.time() - srv.last_used > IDLE_SHUTDOWN_SECONDS:
                    self.forget(srv)
                    await srv.stop()

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        await asyncio.gather(*(s.stop() for s in self._servers.values()), return_exceptions=True)
        self._servers.clear()


registry = LspRegistry()


# ---------------------------------------------------------------------------
# one browser tab
# ---------------------------------------------------------------------------
class LspClient:
    """The server side of one ``/ws/lsp`` connection."""

    def __init__(self, roots: Callable[[], list[tuple[str, str]]]):
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.servers: dict[str, LspServer] = {}
        self._roots = roots  # -> [(root, project_dir)] of every loaded project
        self._allowed: set[str] = set()

    def post(self, msg: dict[str, Any]) -> None:
        self.queue.put_nowait(msg)

    def allow_locations(self, result: Any) -> None:
        items = result if isinstance(result, list) else [result]
        for loc in items:
            if isinstance(loc, dict):
                uri = loc.get("targetUri") or loc.get("uri")
                if uri and uri.startswith("file:"):
                    self._allowed.add(os.path.realpath(uri_to_path(uri)))

    def _project_for(self, path: str) -> tuple[str, str] | None:
        real = os.path.realpath(path)
        best = None
        for root, project_dir in self._roots():
            if real == root or real.startswith(root + os.sep):
                if best is None or len(root) > len(best[0]):
                    best = (root, project_dir)
        return best

    async def handle(self, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        if op == "attach":
            await self._attach(msg.get("language") or "", msg.get("root") or "")
        elif op == "rpc":
            srv = self.servers.get(msg.get("key") or "")
            if srv:
                try:
                    await srv.from_client(self, msg.get("msg") or {})
                except ConnectionError:
                    self.post({"op": "exit", "key": srv.key})
        elif op == "read":
            self.post({"op": "read", "id": msg.get("id"), **self._read(msg.get("uri") or "")})

    async def _attach(self, language: str, root: str) -> None:
        reply = {"op": "attached", "language": language, "root": root}
        owner = self._project_for(root) if root else None
        if not owner:
            self.post({**reply, "error": "not a project directory"})
            return
        srv = await registry.get(language, os.path.realpath(root), owner[1])
        if not srv:
            self.post({**reply, "error": f"no language server for {language}"})
            return
        await srv.ready.wait()
        if srv.failed:
            self.post({**reply, "error": srv.failed})
            return
        srv.clients.add(self)
        self.servers[srv.key] = srv
        self.post({**reply, "key": srv.key, "server": Path(srv.argv[0]).name, "capabilities": srv.capabilities})

    def _read(self, uri: str) -> dict[str, Any]:
        path = os.path.realpath(uri_to_path(uri))
        if path not in self._allowed and not self._project_for(path):
            return {"error": "not readable"}
        try:
            if os.path.getsize(path) > MAX_READ_BYTES:
                return {"error": "file too large"}
            with open(path, encoding="utf-8", errors="replace") as f:
                return {"text": f.read(), "path": path}
        except OSError as e:
            return {"error": str(e)}

    async def close(self) -> None:
        for srv in list(self.servers.values()):
            await srv.detach(self)
        self.servers.clear()
