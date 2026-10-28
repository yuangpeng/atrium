"""Raw config files editable from the Settings dialog.

Atrium does not wrap harness settings in forms: the dialog edits the real
files (``~/.atrium/settings.json``, ``~/.claude/settings.json``,
``~/.codex/config.toml`` and their project-level counterparts). Every save is
validated (JSON / TOML), conflict-checked against the version the editor
loaded, and preceded by a backup in ``~/.atrium/backups/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import BACKUPS_DIR, SETTINGS_PATH, atomic_write

MAX_BACKUPS_PER_FILE = 20


@dataclass
class ConfigFile:
    id: str
    label: str
    path: str
    language: str  # json | toml
    scope: str  # user | project | local
    harness: str  # atrium | claude-code | codex
    hint: str = ""
    secret: bool = False  # created with mode 600

    def describe(self) -> dict[str, Any]:
        d = asdict(self)
        d["exists"] = os.path.exists(self.path)
        return d


class ConfigConflict(Exception):
    pass


class ConfigInvalid(ValueError):
    pass


def atrium_config_file() -> ConfigFile:
    return ConfigFile(
        id="atrium",
        label="Atrium",
        path=str(SETTINGS_PATH),
        language="json",
        scope="user",
        harness="atrium",
        hint="Defaults for new agents per harness, Tracker / Git Agent, web tool keys. Applies immediately.",
        secret=True,
    )


def all_config_files(project_dir: str | None) -> list[ConfigFile]:
    from .harness import all_harnesses

    files = [atrium_config_file()]
    for h in all_harnesses():
        files += h.config_files(project_dir)
    return files


def find(file_id: str, project_dir: str | None) -> ConfigFile:
    for f in all_config_files(project_dir):
        if f.id == file_id:
            return f
    raise FileNotFoundError(f"unknown config file: {file_id}")


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def read(f: ConfigFile) -> dict[str, Any]:
    p = Path(f.path)
    text = p.read_text() if p.exists() else ""
    return {**f.describe(), "content": text, "sha": _sha(text) if p.exists() else None}


def validate(f: ConfigFile, text: str) -> None:
    if not text.strip():
        return
    try:
        if f.language == "json":
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ConfigInvalid("the top level must be a JSON object")
        elif f.language == "toml":
            tomllib.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigInvalid(f"Invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigInvalid(f"Invalid TOML: {e}") from e


def _backup(f: ConfigFile) -> str | None:
    p = Path(f.path)
    if not p.exists():
        return None
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS_DIR / f"{f.id}-{time.strftime('%Y%m%d-%H%M%S')}{p.suffix}"
    dest.write_bytes(p.read_bytes())
    os.chmod(dest, 0o600)
    old = sorted(BACKUPS_DIR.glob(f"{f.id}-*{p.suffix}"))
    for stale in old[:-MAX_BACKUPS_PER_FILE]:
        stale.unlink(missing_ok=True)
    return str(dest)


def write(f: ConfigFile, text: str, base_sha: str | None) -> dict[str, Any]:
    p = Path(f.path)
    current = p.read_text() if p.exists() else None
    if current is not None and base_sha and _sha(current) != base_sha:
        raise ConfigConflict(f"{f.path} changed on disk since it was opened")
    validate(f, text)
    if text and not text.endswith("\n"):
        text += "\n"
    backup = _backup(f)
    atomic_write(p, text, mode=None if p.exists() else (0o600 if f.secret else 0o644))
    return {**read(f), "backup": backup}
