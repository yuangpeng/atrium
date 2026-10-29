"""Minimal async JSON-RPC client for ``codex app-server`` (stdio, newline-delimited JSON).

One process hosts many threads. Notifications and server-initiated requests are
routed to per-thread listeners by ``params.threadId``; anything else goes to the
global listeners.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, Awaitable, Callable

log = logging.getLogger("atrium.codex.rpc")

Listener = Callable[[str, dict[str, Any]], None]
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]

CLIENT_INFO = {"name": "atrium", "title": "Atrium", "version": "0.1.0"}


class RpcError(RuntimeError):
    def __init__(self, error: dict[str, Any]):
        super().__init__(error.get("message") or str(error))
        self.code = error.get("code")
        self.data = error.get("data")


def bundled_codex() -> tuple[str, str | None] | None:
    """The Codex CLI that comes with Atrium (the ``openai-codex-cli-bin`` wheel) and the directory of
    its helper tools (``rg``), or None when that package isn't installed."""
    try:
        from codex_cli_bin import bundled_codex_path, bundled_path_dir

        path_dir = bundled_path_dir()
        return str(bundled_codex_path()), str(path_dir) if path_dir else None
    except (ImportError, FileNotFoundError):
        return None


class AppServerClient:
    def __init__(self, binary: str = "", extra_args: list[str] | None = None):
        self.binary = binary  # "" = the bundled Codex (else the one on PATH); or a command / path
        self.extra_args = extra_args or []
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._thread_listeners: dict[str, Listener] = {}
        self._thread_handlers: dict[str, RequestHandler] = {}
        self._global_listeners: list[Listener] = []
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self.initialize_result: dict[str, Any] | None = None
        self.stderr_tail: list[str] = []

    # ---- lifecycle ---------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def resolve_binary(self) -> str | None:
        if not self.binary:
            bundled = bundled_codex()
            return bundled[0] if bundled else shutil.which("codex")
        b = os.path.expanduser(self.binary)
        if os.path.isabs(b):
            return b if os.access(b, os.X_OK) else None
        return shutil.which(b)

    def not_found(self) -> str:
        if not self.binary:
            return "Codex CLI not found: the bundled one (openai-codex-cli-bin) isn't installed (run make install) and there is no codex on PATH."
        return f"Codex CLI not found ({self.binary}). Install it, or clear harnesses.codex.binary in Atrium settings to use the bundled one."

    async def ensure_started(self) -> None:
        async with self._lock:
            if self.alive:
                return
            path = self.resolve_binary()
            if not path:
                raise FileNotFoundError(self.not_found())
            env = None
            bundled = bundled_codex()
            if bundled and bundled[1] and path == bundled[0]:  # its helper tools go first on PATH, as in OpenAI's own SDK
                env = {**os.environ, "PATH": os.pathsep.join(p for p in (bundled[1], os.environ.get("PATH")) if p)}
            self.proc = await asyncio.create_subprocess_exec(
                path, "app-server", *self.extra_args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, limit=64 * 1024 * 1024,
            )
            self._reader = asyncio.create_task(self._read_loop(), name="codex-rpc-reader")
            self._stderr_task = asyncio.create_task(self._stderr_loop(), name="codex-rpc-stderr")
            self.initialize_result = await self._request_raw(
                "initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": True}}, timeout=60
            )
            await self.notify("initialized", {})

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                self.proc.kill()
        if self._reader:
            self._reader.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

    # ---- routing -----------------------------------------------------------
    def add_thread(self, thread_id: str, listener: Listener, handler: RequestHandler) -> None:
        self._thread_listeners[thread_id] = listener
        self._thread_handlers[thread_id] = handler

    def remove_thread(self, thread_id: str) -> None:
        self._thread_listeners.pop(thread_id, None)
        self._thread_handlers.pop(thread_id, None)

    def add_listener(self, listener: Listener) -> None:
        self._global_listeners.append(listener)

    # ---- io ----------------------------------------------------------------
    async def _write(self, msg: dict[str, Any]) -> None:
        if not self.alive:
            raise RuntimeError("codex app-server is not running")
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            self.proc.stdin.write(data)  # type: ignore[union-attr]
            await self.proc.stdin.drain()  # type: ignore[union-attr]

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"method": method, "params": params or {}})

    async def _request_raw(self, method: str, params: dict[str, Any] | None, timeout: float | None) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"method": method, "id": rid, "params": params or {}})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = 120) -> Any:
        await self.ensure_started()
        return await self._request_raw(method, params, timeout)

    async def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            self.stderr_tail = (self.stderr_tail + [line])[-40:]
            log.debug("codex stderr: %s", line)

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("non-json from codex: %s", line[:200])
                    continue
                self._dispatch(msg)
        except Exception:
            log.exception("codex reader crashed")
        finally:
            err = RuntimeError("codex app-server exited" + (f": {self.stderr_tail[-1]}" if self.stderr_tail else ""))
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            for listener in list(self._thread_listeners.values()) + self._global_listeners:
                try:
                    listener("atrium/disconnected", {})
                except Exception:
                    pass

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and "method" not in msg:  # response
            fut = self._pending.get(msg["id"])
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RpcError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
            return
        method = msg.get("method", "")
        params = msg.get("params") or {}
        tid = params.get("threadId") or params.get("conversationId") or (params.get("thread") or {}).get("id")
        if "id" in msg:  # server -> client request
            asyncio.create_task(self._serve(msg["id"], method, params, tid))
            return
        listener = self._thread_listeners.get(tid) if tid else None
        targets = [listener] if listener else []
        targets += self._global_listeners
        for t in targets:
            try:
                t(method, params)
            except Exception:
                log.exception("listener failed for %s", method)

    async def _serve(self, rid: Any, method: str, params: dict[str, Any], tid: str | None) -> None:
        handler = self._thread_handlers.get(tid) if tid else None
        try:
            if handler is None:
                if method == "currentTime/read":
                    import time

                    result: Any = {"unixTimeMs": int(time.time() * 1000)}
                else:
                    raise RuntimeError(f"no handler for {method}")
            else:
                result = await handler(method, params)
            await self._write({"id": rid, "result": result})
        except Exception as e:
            log.warning("server request %s failed: %s", method, e)
            try:
                await self._write({"id": rid, "error": {"code": -32000, "message": str(e)}})
            except Exception:
                pass
