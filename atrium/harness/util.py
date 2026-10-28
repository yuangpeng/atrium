"""Helpers shared by harness adapters."""

from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime
from typing import Any

from .base import Attachment

MAX_RESULT_CHARS = 20_000


def short(s: Any, n: int) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def clip(s: str, n: int = MAX_RESULT_CHARS) -> str:
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n} more chars]"


def hhmm(iso: str | None) -> str:
    """ISO timestamp (usually UTC) → local "HH:MM" for tracker digests."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return iso[11:16]


def iso_to_epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Non-image attachments are passed to agents as paths in a small trailer appended to
# the prompt; history parsers strip it again and show the files as attachments.
_ATTACHED_RE = re.compile(r"\n*<atrium-attachments>\n(.*?)\n</atrium-attachments>\s*$", re.S)


def attachment_note(files: list[Attachment]) -> str:
    if not files:
        return ""
    return "\n\n<atrium-attachments>\n" + "\n".join(f"- {a.path}" for a in files) + "\n</atrium-attachments>"


def split_attachment_note(text: str) -> tuple[str, list[dict[str, Any]]]:
    m = _ATTACHED_RE.search(text)
    if not m:
        return text, []
    atts = []
    for line in m.group(1).splitlines():
        path = line.strip().lstrip("- ").strip()
        if path:
            name = re.sub(r"^[0-9a-f]{12}__", "", os.path.basename(path))  # upload ids are "<sha12>__<name>"
            atts.append({"name": name, "path": path, "mime": mimetypes.guess_type(name)[0] or "application/octet-stream"})
    return text[: m.start()], atts


def strict_schema(schema: Any) -> Any:
    """OpenAI-style strict JSON schema: closed objects, every property required."""
    if isinstance(schema, dict):
        out = {k: strict_schema(v) for k, v in schema.items()}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out
    if isinstance(schema, list):
        return [strict_schema(x) for x in schema]
    return schema


def parse_json_loose(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("model did not return JSON")


def upload_url(path: str | None) -> str | None:
    """URL under which the UI can show an uploaded attachment (only files in ~/.atrium/uploads)."""
    from ..config import UPLOADS_DIR

    if not path:
        return None
    p = os.path.realpath(path)
    if os.path.dirname(p) == os.path.realpath(UPLOADS_DIR):
        return f"/api/uploads/{os.path.basename(p)}"
    return None
