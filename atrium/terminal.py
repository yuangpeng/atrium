"""Terminals for the workspace's Terminal panel: a shell on a pseudo-terminal, one per tab.

A terminal belongs to a project and starts in one of its roots (the project directory or a worktree). It runs
on the server, not in the page: any number of pages attach to it over ``/ws/terminal/<pid>/<tid>`` and are
first sent its recent output, so a reload, a phone's dropped connection or another device carries on where it
was. It ends when its tab is closed, when its shell exits (an exit status other than 0 keeps the tab, to be
read), or when Atrium stops.

One WebSocket per attached page. Binary frames are the terminal's bytes, both ways: keys in, output out.
Text frames are JSON: from the page ``{"type": "resize", "cols", "rows"}``; to the page ``hello`` (the
terminal's size, its recent output follows), ``ready`` (live from here), ``size`` (another page resized it)
and ``exit`` (``code``). The page that attached, resized or typed last sets the size, as in tmux.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import secrets
import shutil
import signal
import struct
import sys
import termios
import time
from collections import deque
from typing import Any, Callable

from . import __version__
from .config import config
from .events import hub

log = logging.getLogger("atrium.terminal")

REPLAY_BYTES = 1024 * 1024  # recent output, for the pages that attach later
PAUSE_BYTES = 256 * 1024  # every attached page this far behind: stop reading, the program waits for them
RESYNC_BYTES = 2 * 1024 * 1024  # a page this far behind the others is started over (a fresh replay)
READ_BYTES = 65536
MAX_INPUT_BYTES = 1024 * 1024  # keys and pastes the shell hasn't taken yet
KILL_AFTER_SECONDS = 3  # a shell still there this long after the hangup is killed

# The shell is started through Python: os.login_tty makes the pseudo-terminal its controlling terminal (job
# control, Ctrl-C reaching the command), which subprocess can't do and macOS doesn't do on its own, without
# forking this multi-threaded server. Signals go back to their defaults first: Python ignores SIGPIPE, and an
# Atrium started with nohup or `&` ignores SIGHUP or SIGINT — ignored signals stay ignored across exec.
_TRAMPOLINE = """
import os, signal, sys
for s in signal.valid_signals():
    try:
        signal.signal(s, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
signal.pthread_sigmask(signal.SIG_SETMASK, ())
os.login_tty(0)
os.execv(sys.argv[1], sys.argv[2:])
"""
# All a shell gets from Atrium's own environment: who you are, the locale, the temp directory, the ssh agent,
# proxies, the desktop session (Linux), and which Atrium this is. A shell starts fresh, as in a new terminal
# window, and its login profile sets it up: Atrium is often started from where a terminal mustn't inherit from,
# an agent's session (CLAUDE_CODE_*, AI_AGENT), tmux or iTerm, its own virtualenv (uv would install there).
_PASSED_ON = {"HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LANGUAGE", "SSH_AUTH_SOCK", "__CF_USER_TEXT_ENCODING",
              "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "ATRIUM_HOME"}
_PROXIES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}  # either case
_LOGIN_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"  # until the profile adds to it, as after login(1)


class TerminalError(Exception):
    """A terminal can't be started: turned off, or no shell."""


def _shell() -> tuple[str, list[str]]:
    """The program and argv of a new terminal. ``terminal.shell``: "" is $SHELL; a shell (name or path) starts
    as a login shell (argv[0] "-zsh", as login(1) starts it: the profile that sets PATH is read, also when
    Atrium itself was started without one); an argument list runs as given."""
    setting = config.get("terminal.shell") or ""
    if isinstance(setting, list) and setting:
        exe = shutil.which(str(setting[0]))
        if not exe:
            raise TerminalError(f"terminal.shell: {setting[0]} not found")
        return exe, [str(a) for a in setting]
    for cand in [setting] if setting else [os.environ.get("SHELL", ""), "/bin/zsh", "/bin/bash", "/bin/sh"]:
        exe = shutil.which(cand) if cand else None
        if exe:
            return exe, ["-" + os.path.basename(exe)]
    raise TerminalError(f"terminal.shell: {setting} not found" if setting else "no shell found: set terminal.shell")


def _env(cwd: str) -> dict[str, str]:
    # the locale's LC_* too, but not iTerm's LC_TERMINAL: programs would take this terminal for iTerm (inline images)
    env = {k: v for k, v in os.environ.items()
           if k in _PASSED_ON or k.lower() in _PROXIES or (k.startswith("LC_") and not k.startswith("LC_TERMINAL"))}
    env.update(PATH=_LOGIN_PATH, TERM="xterm-256color", COLORTERM="truecolor", TERM_PROGRAM="Atrium", TERM_PROGRAM_VERSION=__version__, PWD=cwd)
    if not (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG")):  # e.g. started by launchd: not even UTF-8
        env["LANG"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return env


def _clamp(cols: int, rows: int) -> tuple[int, int]:
    return max(2, min(int(cols), 1000)), max(1, min(int(rows), 1000))


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _exit_line(code: int) -> bytes:
    why = f"killed by signal {-code}" if code < 0 else f"exited with code {code}"
    return f"\r\n\x1b[2m[process {why}]\x1b[0m\r\n".encode()


class Client:
    """A page attached to a terminal: what it still has to be sent, in order, output run together.
    ``None`` in the queue: the terminal is gone, close the socket."""

    def __init__(self, term: Terminal) -> None:
        self.term = term
        self.queue: deque[bytearray | dict[str, Any] | None] = deque()
        self.queued = 0  # bytes of output in the queue
        self._wake = asyncio.Event()

    def put(self, item: bytes | bytearray | dict[str, Any] | None) -> None:
        if isinstance(item, (bytes, bytearray)):
            if not item:
                return
            last = self.queue[-1] if self.queue else None
            if isinstance(last, bytearray):
                last += item
            else:
                self.queue.append(bytearray(item))
            self.queued += len(item)
        else:
            self.queue.append(item)
        self._wake.set()

    def start_over(self) -> None:
        """The terminal from scratch: its size, then its recent output. On attaching, and when far behind."""
        t = self.term
        self.queue.clear()
        self.queued = 0
        self.put({"type": "hello", "cols": t.cols, "rows": t.rows, "exit_code": t.exit_code})
        self.put(t.replay)
        self.put({"type": "ready"})

    async def take(self) -> list[bytearray | dict[str, Any] | None]:
        while not self.queue:
            self._wake.clear()
            await self._wake.wait()
        items = list(self.queue)
        self.queue.clear()
        self.queued = 0
        self.term.resume()
        return items


class Terminal:
    def __init__(self, project_id: str, root: str, cols: int, rows: int, on_exit: Callable[[Terminal], None]):
        self.id = secrets.token_hex(6)
        self.project_id = project_id
        self.root = root
        self.cols, self.rows = _clamp(cols, rows)
        self.created_at = time.time()
        self.shell = ""
        self.exit_code: int | None = None  # asyncio's: negative = killed by that signal
        self.replay = bytearray()
        self.clients: set[Client] = set()
        self._on_exit = on_exit
        self._proc: asyncio.subprocess.Process | None = None
        self._waiter: asyncio.Task[None] | None = None
        self._fd = -1  # the pseudo-terminal's master side
        self._tty = ""  # its device, e.g. ttys012 / pts/3
        self._input = bytearray()
        self._reading = self._writing = self._eof = self._closing = False

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "root": self.root, "shell": self.shell, "cols": self.cols, "rows": self.rows,
                "created_at": self.created_at, "exit_code": self.exit_code}

    async def start(self) -> None:
        exe, argv = _shell()
        master, slave = os.openpty()
        try:
            _set_size(slave, self.cols, self.rows)
            self._tty = os.ttyname(slave).removeprefix("/dev/")
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", "-c", _TRAMPOLINE, exe, *argv,
                stdin=slave, stdout=slave, stderr=slave, cwd=self.root, env=_env(self.root),
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._fd = master
        self.shell = os.path.basename(exe)
        os.set_blocking(master, False)
        self._set_reading(True)
        self._waiter = asyncio.create_task(self._wait())

    # ---- output ------------------------------------------------------------
    def _set_reading(self, on: bool) -> None:
        if self._fd < 0 or on == self._reading or (on and self._eof):
            return
        loop = asyncio.get_running_loop()
        if on:
            loop.add_reader(self._fd, self._readable)
        else:
            loop.remove_reader(self._fd)
        self._reading = on

    def _readable(self) -> None:
        try:
            data = os.read(self._fd, READ_BYTES)
        except BlockingIOError:
            return
        except OSError:  # EIO: nothing has the terminal open any more (Linux)
            data = b""
        if not data:  # the shell is gone; _wait reaps it
            self._eof = True
            self._set_reading(False)
            return
        self._output(data)

    def _output(self, data: bytes) -> None:
        self.replay += data
        if len(self.replay) > REPLAY_BYTES:
            # down to 3/4, from the start of a line: less likely to cut an escape sequence or a character in two
            cut = len(self.replay) - REPLAY_BYTES * 3 // 4
            nl = self.replay.find(b"\n", cut, cut + 4096)
            del self.replay[: nl + 1 if nl >= 0 else cut]
        for c in self.clients:
            if c.queued > RESYNC_BYTES:  # can't keep up with the others: show it where things are now
                c.start_over()
            else:
                c.put(data)
        if self.clients and all(c.queued > PAUSE_BYTES for c in self.clients):
            self._set_reading(False)  # nobody keeps up: the program waits (its writes block) until a page catches up

    def resume(self) -> None:
        if not self.clients or any(c.queued <= PAUSE_BYTES for c in self.clients):
            self._set_reading(True)

    # ---- input -------------------------------------------------------------
    def write(self, data: bytes) -> None:
        if self._fd < 0 or len(self._input) + len(data) > MAX_INPUT_BYTES:
            return
        self._input += data
        self._flush()

    def _flush(self) -> None:
        while self._input:
            try:
                n = os.write(self._fd, self._input)
            except BlockingIOError:
                break
            except OSError:  # the shell is gone
                self._input.clear()
                break
            del self._input[:n]
        want = bool(self._input)
        if want != self._writing:
            loop = asyncio.get_running_loop()
            if want:
                loop.add_writer(self._fd, self._flush)
            else:
                loop.remove_writer(self._fd)
            self._writing = want

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = _clamp(cols, rows)
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        if self._fd >= 0:
            try:
                _set_size(self._fd, cols, rows)  # the kernel tells the program (SIGWINCH)
            except OSError:
                pass
        for c in self.clients:
            c.put({"type": "size", "cols": cols, "rows": rows})

    # ---- pages -------------------------------------------------------------
    def attach(self) -> Client:
        c = Client(self)
        c.start_over()
        self.clients.add(c)
        return c

    def detach(self, c: Client) -> None:
        self.clients.discard(c)
        self.resume()

    # ---- the end -------------------------------------------------------------
    async def foreground(self) -> str | None:
        """What runs in the foreground, other than the shell itself (e.g. "npm"); None while it waits for a
        command. From ps: macOS only tells a terminal's foreground process group to its own session."""
        if self.exit_code is not None or not self._proc or not self._tty:
            return None
        try:
            ps = await asyncio.create_subprocess_exec(
                "ps", "-o", "pid=,tpgid=,comm=", "-t", self._tty, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(ps.communicate(), 5)
        except (OSError, asyncio.TimeoutError):
            return None
        procs = [r for r in (line.split(None, 2) for line in out.decode(errors="replace").splitlines())
                 if len(r) == 3 and r[0].isdigit() and r[1].lstrip("-").isdigit()]
        fg = int(procs[0][1]) if procs else 0  # every process on the terminal reports its foreground group
        if fg <= 0 or fg == self._proc.pid:
            return None
        leader = next((r for r in procs if int(r[0]) == fg), None)
        return os.path.basename(leader[2].strip()) if leader else "a command"

    async def _wait(self) -> None:
        assert self._proc
        code = await self._proc.wait()
        if not self._eof:  # what it wrote last can still be in the terminal (bounded: a job left behind may write on)
            for _ in range(16):
                try:
                    data = os.read(self._fd, READ_BYTES)
                except OSError:
                    break
                if not data:
                    break
                self._output(data)
        self.exit_code = code
        self._release()
        if not self._closing:
            self._output(_exit_line(code))
        for c in self.clients:
            c.put({"type": "exit", "code": code})
        self._on_exit(self)

    def _release(self) -> None:
        if self._fd < 0:
            return
        loop = asyncio.get_running_loop()
        if self._reading:
            loop.remove_reader(self._fd)
        if self._writing:
            loop.remove_writer(self._fd)
        self._reading = self._writing = False
        os.close(self._fd)  # hangs the terminal up: what still runs on it gets SIGHUP
        self._fd = -1
        self._input.clear()

    def _signal(self, sig: int) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                os.killpg(self._proc.pid, sig)  # the shell leads its own session and process group
            except OSError:
                pass

    async def close(self) -> None:
        """Hang up, as closing a terminal window does: SIGHUP, and SIGKILL for a shell still there after a while."""
        self._closing = True
        for c in self.clients:
            c.put(None)
        if self.exit_code is None and self._waiter:
            self._signal(signal.SIGHUP)
            self._release()
            try:
                await asyncio.wait_for(asyncio.shield(self._waiter), KILL_AFTER_SECONDS)
            except asyncio.TimeoutError:
                self._signal(signal.SIGKILL)


class TerminalManager:
    """Every project's terminals."""

    def __init__(self) -> None:
        self._terms: dict[str, dict[str, Terminal]] = {}

    def available(self) -> bool:
        return os.name == "posix" and hasattr(os, "login_tty") and bool(config.get("terminal.enabled"))

    def list(self, pid: str) -> list[dict[str, Any]]:
        return [t.describe() for t in self._terms.get(pid, {}).values()]

    def get(self, pid: str, tid: str) -> Terminal | None:
        return self._terms.get(pid, {}).get(tid)

    async def create(self, pid: str, root: str, cols: int, rows: int) -> Terminal:
        if not self.available():
            raise TerminalError("the terminal is turned off (terminal.enabled in settings.json)")
        t = Terminal(pid, root, cols, rows, self._exited)
        await t.start()
        self._terms.setdefault(pid, {})[t.id] = t
        self._changed(pid)
        return t

    def _exited(self, t: Terminal) -> None:
        terms = self._terms.get(t.project_id, {})
        if t.exit_code == 0 and terms.get(t.id) is t:  # `exit`: the tab goes; any other ending stays, to be read
            del terms[t.id]
            for c in t.clients:
                c.put(None)
        self._changed(t.project_id)

    async def close(self, pid: str, tid: str) -> None:
        t = self._terms.get(pid, {}).pop(tid, None)
        if t:
            self._changed(pid)
            await t.close()

    async def running(self, pid: str) -> list[str]:
        """The commands running in the foreground of a project's terminals."""
        names = await asyncio.gather(*(t.foreground() for t in self._terms.get(pid, {}).values()))
        return [n for n in names if n]

    async def close_project(self, pid: str) -> None:
        terms = self._terms.pop(pid, {})
        if terms:
            self._changed(pid)
        await asyncio.gather(*(t.close() for t in terms.values()))

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close_project(pid) for pid in list(self._terms)))

    @staticmethod
    def _changed(pid: str) -> None:
        hub.publish(f"project:{pid}", {"type": "terminals_changed"})


terminals = TerminalManager()
