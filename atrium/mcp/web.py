"""Web tools: search via Serper (google.serper.dev), fetch via Jina Reader (r.jina.ai).

Used two ways:
* in-process — ``web_toolset()`` returns harness-neutral ToolSpecs in the ``web``
  namespace: Claude Code sees ``mcp__web__search`` / ``mcp__web__fetch`` (SDK MCP
  server), Codex sees the dynamic tools ``web.search`` / ``web.fetch``;
* stdio MCP server — ``atrium-web-mcp`` (or ``python -m atrium.mcp.web``) for
  any other MCP client. Keys come from SERPER_API_KEY / JINA_API_KEY or the
  ``web`` section of ``~/.atrium/settings.json``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..harness.base import ToolSpec

WEB_SERVER = "web"
SERPER_BASE = "https://google.serper.dev"
JINA_READER = "https://r.jina.ai/"
TIMEOUT = httpx.Timeout(45.0, connect=10.0)


def _fmt_serper(kind: str, data: dict[str, Any]) -> str:
    out: list[str] = []
    ab = data.get("answerBox")
    if ab:
        out.append(f"**Answer box:** {ab.get('answer') or ab.get('snippet') or ''} {ab.get('link') or ''}".strip())
    kg = data.get("knowledgeGraph")
    if kg:
        out.append(f"**Knowledge graph:** {kg.get('title', '')} — {kg.get('description', '')} {kg.get('website', '')}".strip())
    key = {"news": "news", "scholar": "organic", "images": "images"}.get(kind, "organic")
    for i, r in enumerate(data.get(key) or [], 1):
        line = f"{i}. **{r.get('title', '')}**\n   {r.get('link') or r.get('imageUrl', '')}"
        meta = " · ".join(str(x) for x in (r.get("date"), r.get("source"), r.get("publicationInfo")) if x)
        if meta:
            line += f"\n   {meta}"
        if r.get("snippet"):
            line += f"\n   {r['snippet']}"
        out.append(line)
    paa = data.get("peopleAlsoAsk") or []
    if paa:
        out.append("**People also ask:**\n" + "\n".join(f"- {p.get('question')} — {p.get('snippet', '')} ({p.get('link', '')})" for p in paa[:4]))
    related = data.get("relatedSearches") or []
    if related:
        out.append("**Related searches:** " + "; ".join(r.get("query", "") for r in related[:6]))
    return "\n\n".join(out) or "No results."


def web_toolset(settings: dict[str, Any]) -> list[ToolSpec]:
    """``settings`` is the ``web`` section of Atrium's settings."""
    serper_key = settings.get("serper_api_key") or ""
    jina_key = settings.get("jina_api_key") or ""
    tools: list[ToolSpec] = []

    async def search(args: dict[str, Any]) -> dict[str, Any] | str:
        if not serper_key:
            return {"text": "web search is not configured: set SERPER_API_KEY (or the key in Atrium settings).", "is_error": True}
        kind = args.get("type") or "search"
        payload: dict[str, Any] = {"q": args["query"], "num": min(int(args.get("num") or 8), 20)}
        for k in ("gl", "hl", "tbs", "page"):
            if args.get(k):
                payload[k] = args[k]
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{SERPER_BASE}/{kind}", headers={"X-API-KEY": serper_key, "Content-Type": "application/json"}, content=json.dumps(payload))
        if r.status_code != 200:
            return {"text": f"Serper error {r.status_code}: {r.text[:500]}", "is_error": True}
        return _fmt_serper(kind, r.json())

    async def fetch(args: dict[str, Any]) -> dict[str, Any] | str:
        url = args["url"].strip()
        max_chars = max(1000, min(int(args.get("max_chars") or 20000), 100_000))
        offset = max(0, int(args.get("offset") or 0))
        headers = {"X-Return-Format": args.get("format") or "markdown", "Accept": "text/plain"}
        if jina_key:
            headers["Authorization"] = f"Bearer {jina_key}"
        if args.get("selector"):
            headers["X-Target-Selector"] = args["selector"]
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            r = await client.get(JINA_READER + url, headers=headers)
        if r.status_code != 200:
            return {"text": f"Jina Reader error {r.status_code}: {r.text[:500]}", "is_error": True}
        text = r.text
        chunk = text[offset : offset + max_chars]
        rest = len(text) - (offset + len(chunk))
        if rest > 0:
            chunk += f"\n\n[… {rest} more characters. Call fetch again with offset={offset + len(chunk)} to continue.]"
        return chunk or "(empty page)"

    tools.append(
        ToolSpec(
            name="search",
            description=(
                "Search the web (Google results via Serper). Returns titles, links and snippets. "
                "Use type='news' for recent news, 'scholar' for papers. tbs: qdr:d|qdr:w|qdr:m|qdr:y limits recency."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "num": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                    "type": {"type": "string", "enum": ["search", "news", "scholar"], "default": "search"},
                    "gl": {"type": "string", "description": "Country code, e.g. us, cn"},
                    "hl": {"type": "string", "description": "Language code, e.g. en, zh-cn"},
                    "tbs": {"type": "string", "description": "Time filter, e.g. qdr:w"},
                    "page": {"type": "integer", "minimum": 1},
                },
                "required": ["query"],
            },
            handler=search,
            read_only=True,
        )
    )
    tools.append(
        ToolSpec(
            name="fetch",
            description=(
                "Fetch a web page (or PDF) and return its main content as clean markdown (via Jina Reader). "
                "Long pages are paginated: pass offset to continue."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 20000},
                    "offset": {"type": "integer", "default": 0},
                    "selector": {"type": "string", "description": "Optional CSS selector to extract"},
                    "format": {"type": "string", "enum": ["markdown", "text", "html"], "default": "markdown"},
                },
                "required": ["url"],
            },
            handler=fetch,
            read_only=True,
        )
    )
    return tools


def main() -> None:
    """Run the web tools as a stdio MCP server."""
    import anyio
    from mcp.server.stdio import stdio_server

    from ..config import config
    from ..harness.claude_code import to_sdk_server

    server = to_sdk_server(WEB_SERVER, web_toolset(config.get("web") or {}))["instance"]

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
