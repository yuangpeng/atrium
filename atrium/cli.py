"""``atrium`` command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from typing import Any

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def main() -> None:
    ap = argparse.ArgumentParser(prog="atrium", description="Project-centric workbench for coding agents")
    ap.add_argument("project", nargs="?", help="open this directory as a project")
    ap.add_argument("--host", default=os.environ.get("ATRIUM_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ATRIUM_PORT", "8787")))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reload", action="store_true", help="auto-reload the backend on code changes (development)")
    ap.add_argument("--log-level", default="info")
    ap.add_argument("--set-password", action="store_true", help="set the password of the login page, then exit")
    ap.add_argument("--token", action="store_true", help="print the API token for scripts (Authorization: Bearer <token>), then exit")
    ap.add_argument("--no-auth", action="store_true", help="no login, for scratch instances (only with a loopback --host)")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.set_password:
        return _set_password()
    if args.token:
        print(_auth()["token"])
        return

    os.environ.pop("ATRIUM_NO_AUTH", None)  # only this flag turns the login off, never an inherited variable
    if args.no_auth:
        if args.host not in LOOPBACK_HOSTS:
            ap.error("--no-auth needs a loopback --host (127.0.0.1, localhost or ::1)")
        os.environ["ATRIUM_NO_AUTH"] = "1"  # for the server process (see auth.py), which drops it
        login = "No login (--no-auth): it only answers to localhost"
    else:
        from .auth import AUTH_PATH

        generated = _auth().get("generated_password")
        if not generated:
            login = ""
        elif sys.stdout.isatty():  # not into log files
            login = f"Password: {generated}   (made up; set your own with: atrium --set-password)"
        else:
            login = f"Password: made up, in {AUTH_PATH} (set your own with: atrium --set-password)"

    url = f"http://{args.host}:{args.port}/"
    os.environ.setdefault("ATRIUM_URL", url)  # links in desktop notifications
    if args.project:
        from .config import config

        entry = config.add_project(args.project)
        url += f"?project={entry['id']}"

    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()

    import uvicorn

    print(f"\n  Atrium is running at {url}" + (f"\n  {login}" if login else "") + "\n")
    uvicorn.run("atrium.server:app", host=args.host, port=args.port, reload=args.reload, log_level=args.log_level)


def _auth() -> dict[str, Any]:
    """auth.json, created on a first run (see auth.py)."""
    from .auth import auth

    try:
        return auth.ensure()
    except RuntimeError as e:  # unreadable: no made-up password over it
        sys.exit(str(e))


def _set_password() -> None:
    import getpass

    from .auth import auth

    password = getpass.getpass("New password for Atrium: ")
    if len(password) < 8:
        sys.exit("Use at least 8 characters.")
    if getpass.getpass("Again: ") != password:
        sys.exit("The two don't match.")
    auth.set_password(password)
    print(f"Saved in {auth.path}, with a new API token. Browsers logged in before, and scripts with the old token, have to log in again.")


if __name__ == "__main__":
    main()
