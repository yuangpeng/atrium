# Notes for agents working on Atrium

Sessions started from Atrium's UI are child processes of the Atrium server, so you may well be
running inside the app you are editing.

## Run

- Setup: `make install`, then `make build` (builds the UI into `atrium/static`).
- Start: `make run` (port 8787; if it is taken, `make run PORT=8790`).
  Development: `make dev-backend` + `make dev-web` (Vite on 5173).
- Check: `curl -s --noproxy '*' http://127.0.0.1:<port>/api/meta` lists each harness and
  whether it is available.

## Restarting stops every agent

- Killing or restarting the Atrium server ends the turn in progress of every live session,
  yours included; sessions can be resumed from the UI afterwards. Python changes only take
  effect after a restart, so ask the human to restart instead of doing it yourself.
- Under `make dev-backend` (auto-reload), every saved `.py` file restarts the server.
- UI changes show up after `make build`. It type-checks first and stops on errors; then it
  empties and rewrites `atrium/static`, and the human's UI changes on their next page reload.
- To try backend changes, start a separate instance with its own state, on a scratch project
  (project state lives in `<project>/.atrium/`, so don't point it at this repo):
  `ATRIUM_HOME=$(mktemp -d) .venv/bin/atrium --port 8790 --no-browser <scratch dir>`

## Keep this in sync

The Makefile is the source of truth. If you change how Atrium is installed, built or run,
update README "Quick start" and this file in the same change.
