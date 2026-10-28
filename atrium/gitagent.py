"""Git Agent support: the ``graph.set_view`` tool lets an agent drive Atrium's commit graph."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .events import hub
from .harness.base import ToolSpec

if TYPE_CHECKING:
    from .project import Project

GIT_SERVER = "graph"


def set_view(project: "Project", branches: list[str] | None, highlight: list[str] | None, note: str | None, source: str) -> dict[str, Any]:
    view = {
        "branches": [b for b in (branches or []) if b],
        "highlight": [h for h in (highlight or []) if h],
        "note": note or "",
        "source": source,
        "updated_at": time.time(),
    }
    project.set_git_view(view)
    hub.publish(f"project:{project.id}", {"type": "git_view", "view": view})
    return view


def git_toolset(project: "Project") -> list[ToolSpec]:
    async def set_view_tool(args: dict[str, Any]) -> str:
        view = set_view(project, args.get("branches"), args.get("highlight"), args.get("note"), source="git-agent")
        return f"Graph now shows {len(view['branches']) or 'all'} branch(es): {', '.join(view['branches']) or '(all refs)'}."

    return [
        ToolSpec(
            name="set_view",
            description=(
                "Control the commit graph displayed to the human in Atrium. branches: refs to include "
                "(local or remote names, e.g. 'main', 'feature/x', 'origin/main'); empty list = all refs. "
                "highlight: commit SHAs or branch names to emphasise. note: short explanation shown above the graph."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "branches": {"type": "array", "items": {"type": "string"}},
                    "highlight": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["branches"],
            },
            handler=set_view_tool,
        )
    ]


GIT_AGENT_ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git log:*)",
    "Bash(git status:*)",
    "Bash(git branch:*)",
    "Bash(git show:*)",
    "Bash(git diff:*)",
    "Bash(git worktree list:*)",
    "Bash(git for-each-ref:*)",
    "Bash(git rev-parse:*)",
    "Bash(git rev-list:*)",
    "Bash(git merge-base:*)",
    "Bash(git remote -v:*)",
    "Bash(git tag:*)",
    "Bash(git shortlog:*)",
    "Bash(git reflog:*)",
]
