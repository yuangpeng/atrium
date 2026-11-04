"""Minimal async JSON-RPC client for an agent that speaks the Agent Client Protocol on stdio (newline-delimited JSON).

One agent process per client. The agent's notifications (``session/update``, ``_x.ai/…`` extensions) go to
``on_notify``; its requests to the client (``session/request_permission``, reverse MCP calls…) to ``on_request``,
whose return value is the result (an exception becomes a JSON-RPC error). When the process exits, pending
requests fail and ``on_notify`` gets ``atrium/disconnected`` (``{"client": <this client>}``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("atrium.acp")

Notify = Callable[[str, dict[str, Any]], None]
Serve = Callable[[str, dict[str, Any]], Awaitable[Any]]


class AcpError(RuntimeError):
    def __init__(self, error: dict[str, Any]):
        data = error.get("data")
        detail = data if isinstance(data, str) else json.dumps(data) if data else ""
        super().__init__(" ".join(x for x in (error.get("message") or "", detail) if x) or str(error))
        self.code = error.get("code")
        self.data = data


class AcpClient:
    def __init__(self, argv: list[str], *, cwd: str, env: dict[str, str], on_notify: Notify, on_request: Serve, name: str = "acp"):
        self.argv, self.cwd, self.env, self.name = argv, cwd, env, name
        self.on_notify, self.on_request = on_notify, on_request
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: list[str] = []
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, limit=64 * 1024 * 1024,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self._tasks = [asyncio.create_task(self._read_loop(), name=f"{self.name}-reader"),
                       asyncio.create_task(self._stderr_loop(), name=f"{self.name}-stderr")]

    async def close(self, grace: float = 5) -> None:
        proc = self.proc
        if proc and proc.returncode is None:
            try:
                assert proc.stdin
                proc.stdin.close()  # the agent exits when its input ends
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass
        for t in self._tasks:
            t.cancel()

    # ---- io ----------------------------------------------------------------
    async def _write(self, msg: dict[str, Any]) -> None:
        proc = self.proc
        if not proc or proc.returncode is not None or not proc.stdin:
            raise RuntimeError(f"{self.name} is not running")
        data = (json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            proc.stdin.write(data)
            await proc.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = 120) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    async def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-40:]
                log.debug("%s stderr: %s", self.name, line)

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
                    log.debug("%s: not JSON: %s", self.name, line[:200])
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except Exception:
            log.exception("%s reader crashed", self.name)
        finally:
            if self.proc:
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=5)
                except Exception:
                    pass
            err = RuntimeError(f"{self.name} exited" + (f": {self.stderr_tail[-1]}" if self.stderr_tail else ""))
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            try:
                self.on_notify("atrium/disconnected", {"client": self})
            except Exception:
                log.exception("%s: disconnect listener failed", self.name)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" not in msg:  # a response
            fut = self._pending.get(x) if isinstance(x := msg.get("id"), int) else None
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(AcpError(msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}))
                else:
                    fut.set_result(msg.get("result"))
            return
        params: dict[str, Any] = x if isinstance(x := msg.get("params"), dict) else {}
        if "id" in msg:  # the agent asks the client
            asyncio.create_task(self._serve(msg["id"], msg["method"], params))
            return
        try:
            self.on_notify(msg["method"], params)
        except Exception:
            log.exception("%s: listener failed for %s", self.name, msg["method"])

    async def _serve(self, rid: Any, method: str, params: dict[str, Any]) -> None:
        try:
            result = await self.on_request(method, params)
            await self._write({"id": rid, "result": result if result is not None else {}})
        except Exception as e:
            if not isinstance(e, NotImplementedError):
                log.warning("%s: %s failed: %s", self.name, method, e)
            try:
                await self._write({"id": rid, "error": {"code": -32601 if isinstance(e, NotImplementedError) else -32603, "message": str(e)}})
            except Exception:
                pass
