"""``atrium`` command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(prog="atrium", description="Project-centric workbench for coding agents")
    ap.add_argument("project", nargs="?", help="open this directory as a project")
    ap.add_argument("--host", default=os.environ.get("ATRIUM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ATRIUM_PORT", "8787")))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="auto-reload the backend on code changes (development)")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    url = f"http://{args.host}:{args.port}/"
    if args.project:
        from .config import config

        entry = config.add_project(args.project)
        url += f"?project={entry['id']}"

    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()

    import uvicorn

    print(f"\n  Atrium is running at {url}\n")
    uvicorn.run("atrium.server:app", host=args.host, port=args.port, reload=args.reload, log_level=args.log_level)


if __name__ == "__main__":
    main()
