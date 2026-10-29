"""Global configuration and the registry of opened projects.

Everything global lives in ``$ATRIUM_HOME`` (default ``~/.atrium``):

    settings.json    Atrium's own settings — edited as a raw file in the UI
                     (next to ~/.claude/settings.json and ~/.codex/config.toml)
    config.json      the list of known projects (app state, not settings)
    backups/         copies of config files taken before every save from the UI
    uploads/         files attached in the composer (pasted images etc.)
    runtime/         scratch cwd for background LLM calls (keeps their
                     transcripts out of the user's project session lists)

Per-project state lives in ``<project>/.atrium/`` (see ``project.py``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

ATRIUM_HOME = Path(os.environ.get("ATRIUM_HOME", Path.home() / ".atrium")).expanduser()
CONFIG_PATH = ATRIUM_HOME / "config.json"
SETTINGS_PATH = ATRIUM_HOME / "settings.json"
RUNTIME_DIR = ATRIUM_HOME / "runtime"
UPLOADS_DIR = ATRIUM_HOME / "uploads"
BACKUPS_DIR = ATRIUM_HOME / "backups"

DEFAULT_SETTINGS: dict[str, Any] = {
    "defaults": {
        "harness": "claude-code",  # harness preselected for new agents
    },
    # Per-harness defaults for new sessions. model/effort "" = the harness's own default
    # (i.e. whatever ~/.claude/settings.json or ~/.codex/config.toml says). Agents start with full access.
    # Codex's binary "" = the one bundled with Atrium (openai-codex-cli-bin); a command or path = your own.
    "harnesses": {
        "claude-code": {"permission_mode": "bypassPermissions", "model": "", "effort": ""},
        "codex": {"enabled": True, "binary": "", "permission_mode": "full-access", "model": "", "effort": ""},
    },
    "sessions": {"idle_close_minutes": 30},
    # Desktop notifications when an agent asks for approval or finishes a turn — skipped while its
    # chat is open in a focused browser tab. macOS: terminal-notifier if installed (click opens the
    # session), else osascript; Linux: notify-send.
    "notifications": {"enabled": True, "permission": True, "turn_end": True, "sound": True},
    # Background Tracker Agent (one per project)
    "tracker": {
        "enabled": True,
        "harness": "claude-code",
        "model": "",  # a small fast model is plenty
        "effort": "low",
        "interval_seconds": 300,
        "after_turn_seconds": 45,  # debounce: re-track shortly after a turn ends
    },
    "git_agent": {"harness": "claude-code", "model": ""},
    # Code Review Agent: one read-only review session per agent. auto: review an agent's new changes
    # when it reports its task done or waiting for the human (board.status); the Review button runs
    # one any time. Nothing reaches the agent until the human sends it.
    "review_agent": {
        "auto": True,
        "harness": "claude-code",
        "model": "",
        "effort": "",
        "language": "",  # what the reviewer writes in; "" = the human's language
        "delay_seconds": 15,  # after the turn ends; skipped if the agent is busy again by then
        "max_concurrent": 2,  # automatic reviews running at once (the Review button doesn't wait)
    },
    # Atrium's web tools (exposed to every agent as web.search / web.fetch)
    "web": {
        "serper_api_key": "",  # env SERPER_API_KEY wins
        "jina_api_key": "",  # env JINA_API_KEY wins (optional, raises rate limits)
        "replace_builtin_web_tools": True,
    },
    # Extra MCP servers for Claude Code sessions ({"name": {"command": ..., "args": [...]}}).
    # Codex sessions use the [mcp_servers] table of ~/.codex/config.toml instead.
    "mcp_servers": {},
    # Language servers behind the editor (semantic highlighting, go to definition, hover, …).
    # commands: {"python": ["pyright-langserver", "--stdio"]} overrides the auto-detected server.
    "lsp": {
        "enabled": True,
        "commands": {},
        "python": {"typeCheckingMode": "standard"},  # off | basic | standard | strict | recommended | all
    },
    "ui": {"theme": "system"},
}

_ENV_SECRETS = {("web", "serper_api_key"): "SERPER_API_KEY", ("web", "jina_api_key"): "JINA_API_KEY"}

# flat keys of the first version → nested paths
_LEGACY_KEYS = {
    "default_harness": "defaults.harness",
    "default_model": "harnesses.claude-code.model",
    "default_permission_mode": "harnesses.claude-code.permission_mode",
    "session_idle_close_minutes": "sessions.idle_close_minutes",
    "tracker_enabled": "tracker.enabled",
    "tracker_interval_seconds": "tracker.interval_seconds",
    "tracker_model": "tracker.model",
    "tracker_after_turn_seconds": "tracker.after_turn_seconds",
    "serper_api_key": "web.serper_api_key",
    "jina_api_key": "web.jina_api_key",
    "replace_builtin_web_tools": "web.replace_builtin_web_tools",
    "extra_mcp_servers": "mcp_servers",
    "theme": "ui.theme",
}


def project_id_for(path: str | Path) -> str:
    real = os.path.realpath(str(path))
    return hashlib.sha1(real.encode()).hexdigest()[:12]


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _set_path(d: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        nxt = d.get(k)
        if not isinstance(nxt, dict):
            nxt = d[k] = {}
        d = nxt
    d[keys[-1]] = value


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".atrium-tmp")
    tmp.write_text(text)
    if mode is not None:
        os.chmod(tmp, mode)
    elif path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)


class Config:
    """Settings (``settings.json``, user-editable) + project registry (``config.json``)."""

    def __init__(self, path: Path = CONFIG_PATH, settings_path: Path = SETTINGS_PATH):
        self.path = path
        self.settings_path = settings_path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"projects": []}
        self._user: dict[str, Any] = {}
        self._settings_mtime = 0.0
        self.settings_error: str | None = None
        self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> None:
        for d in (ATRIUM_HOME, RUNTIME_DIR, UPLOADS_DIR, BACKUPS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self.path.rename(self.path.with_suffix(f".broken-{int(time.time())}.json"))
        self._data.setdefault("projects", [])
        legacy = self._data.pop("settings", None)
        if not self.settings_path.exists():
            seed = copy.deepcopy(DEFAULT_SETTINGS)
            for k, v in (legacy or {}).items():
                if k in _LEGACY_KEYS and v not in ("", None):
                    _set_path(seed, _LEGACY_KEYS[k], v)
            atomic_write(self.settings_path, json.dumps(seed, indent=2, ensure_ascii=False) + "\n", mode=0o600)
        if legacy is not None:
            self._save()
        self._read_settings()

    def _read_settings(self) -> None:
        try:
            st = self.settings_path.stat()
            data = json.loads(self.settings_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("top level must be a JSON object")
            self._user = data
            self.settings_error = None
            self._settings_mtime = st.st_mtime
        except FileNotFoundError:
            self._user = {}
        except (ValueError, json.JSONDecodeError) as e:  # keep the last good settings
            self.settings_error = f"{self.settings_path}: {e}"

    def _fresh(self) -> None:
        """Pick up edits made to settings.json outside Atrium."""
        try:
            if self.settings_path.stat().st_mtime != self._settings_mtime:
                self._read_settings()
        except FileNotFoundError:
            pass

    def _save(self) -> None:
        atomic_write(self.path, json.dumps(self._data, indent=2, ensure_ascii=False), mode=0o600)

    # ---- settings ----------------------------------------------------------
    @property
    def settings(self) -> dict[str, Any]:
        with self._lock:
            self._fresh()
            merged = deep_merge(DEFAULT_SETTINGS, self._user)
            for (sec, key), env in _ENV_SECRETS.items():
                if os.environ.get(env):
                    merged.setdefault(sec, {})[key] = os.environ[env]
            return merged

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.settings
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def harness_defaults(self, harness_id: str) -> dict[str, Any]:
        return dict(self.get(f"harnesses.{harness_id}") or {})

    def public_settings(self) -> dict[str, Any]:
        """Settings safe to send to the browser (secrets reduced to a flag)."""
        s = self.settings
        for (sec, key), _env in _ENV_SECRETS.items():
            v = s.get(sec, {}).get(key) or ""
            s[sec][key] = bool(v)
        s["_error"] = self.settings_error
        return s

    def set(self, dotted: str, value: Any) -> None:
        """Set one key in settings.json (used for small UI preferences such as the theme)."""
        with self._lock:
            self._fresh()
            data = copy.deepcopy(self._user)
            _set_path(data, dotted, value)
            atomic_write(self.settings_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            self._read_settings()

    def reload(self) -> None:
        with self._lock:
            self._read_settings()

    # ---- projects ----------------------------------------------------------
    def projects(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for p in self._data["projects"]:
                out.append({**p, "id": project_id_for(p["path"]), "exists": os.path.isdir(p["path"])})
            out.sort(key=lambda p: p.get("last_opened", 0), reverse=True)
            return out

    def add_project(self, path: str) -> dict[str, Any]:
        real = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(real):
            raise FileNotFoundError(real)
        with self._lock:
            for p in self._data["projects"]:
                if p["path"] == real:
                    p["last_opened"] = time.time()
                    self._save()
                    return {**p, "id": project_id_for(real), "exists": True}
            entry = {"path": real, "name": os.path.basename(real) or real, "added_at": time.time(), "last_opened": time.time()}
            self._data["projects"].append(entry)
            self._save()
            return {**entry, "id": project_id_for(real), "exists": True}

    def touch_project(self, pid: str) -> None:
        with self._lock:
            for p in self._data["projects"]:
                if project_id_for(p["path"]) == pid:
                    p["last_opened"] = time.time()
                    self._save()
                    return

    def remove_project(self, pid: str) -> None:
        with self._lock:
            self._data["projects"] = [p for p in self._data["projects"] if project_id_for(p["path"]) != pid]
            self._save()

    def project_path(self, pid: str) -> str | None:
        with self._lock:
            for p in self._data["projects"]:
                if project_id_for(p["path"]) == pid:
                    return p["path"]
        return None


config = Config()
