# Notes for agents working on Atrium

Sessions started from Atrium's UI are child processes of the Atrium server, so you may well be
running inside the app you are editing.

## Run

- Setup: `make install`, then `make build` (builds the UI into `atrium/static`).
  The Claude Code and Codex CLIs come from the venv (`claude-agent-sdk`, `openai-codex-cli-bin`),
  not from `PATH`, and so does Antigravity's runtime (`google-antigravity`); run `make install` again
  after changing their versions in `pyproject.toml`.
- Start: `make run` (port 8787; if it is taken, `make run PORT=8790`).
  Development: `make dev-backend` + `make dev-web` (Vite on 5173).
- Check: `curl -s --noproxy '*' -H "Authorization: Bearer $(.venv/bin/atrium --token)" http://127.0.0.1:<port>/api/meta`
  lists each harness and whether it is available (for Codex also which CLI runs: `cli.path`,
  `cli.version`). Every request needs a login: the browser's cookie, or this token for scripts
  (`--token` reads `$ATRIUM_HOME/auth.json`: the same `ATRIUM_HOME` as the server's).
- Antigravity reads `~/.gemini/antigravity-cli/settings.json` (the human's real key and relay). For
  tests without model calls, start the instance with `HOME` set to a temp dir whose
  `.gemini/antigravity-cli/settings.json` makes a local fake of the Gemini API the model
  (`"customModelsConfig": {"customModels": {"fake": {"modelName": "gemini-api://http://127.0.0.1:<port>/models/fake"}}}, "model": "fake", "env": {"GEMINI_API_KEY": "fake"}`;
  it gets `POST /v1beta/models/fake:streamGenerateContent?alt=sse`; a relay only gets a key from that file,
  and the runtime won't start without one). Never write the human's file.

## Restarting stops every agent

- Killing or restarting the Atrium server ends the turn in progress of every live session,
  yours included, and every shell in the Terminal tab; sessions can be resumed from the UI
  afterwards, terminals can't. Python changes only take
  effect after a restart, so ask the human to restart instead of doing it yourself.
- Under `make dev-backend` (auto-reload), every saved `.py` file restarts the server.
- UI changes show up after `make build`. It type-checks first and stops on errors; then it
  empties and rewrites `atrium/static`, and the human's UI changes on their next page reload.
- To try backend changes, start a separate instance with its own state, on a scratch project
  (project state lives in `<project>/.atrium/`, so don't point it at this repo):
  `ATRIUM_HOME=$(mktemp -d) .venv/bin/atrium --port 8790 --no-browser --no-auth <scratch dir>`
  (`--no-auth`: no login page, and only requests to localhost are answered; leave it out to test
  the login itself).

## Every UI change has to work on a phone, iPhone Duo included

Atrium is deployed and opened from a phone, so **any** change to `web/` (or to the login page in
`atrium/auth.py`, or the board's HTML in `atrium/board/template.py`) must be checked at the iPhone
Duo's sizes before it is called done. This is not optional polish: a change that only works at
desktop width is unfinished.

Safari 27.1 on iPhone Duo reports these viewports (WebKit's own table; SVH = with the browser's
bars showing):

| Pose | Width | SVH | LVH |
| --- | --- | --- | --- |
| Outer display, portrait | 382 | 580 | 618 |
| Outer display, landscape | 594 | 368 | 406 |
| Inner display, portrait | 669 | 867 | 902 |
| Inner display, landscape | 951 | 585 | 620 |
| One window of two, side by side | 385 | 571 | 609 |

Added to the Home Screen, Atrium opens as an app with no Safari bars, under the device's safe-area
insets: the home indicator along the bottom (34px), and the clock and Wi-Fi down the right edge. The
outer display in portrait gives the page that column as an 84px inset (466 × 678); the inner one in
landscape keeps it out of the page (867 × 669), and paints it in the page's background (measured from
the device's screenshots). The status bar can't be hidden or drawn under: Apple doesn't support it in
Home Screen apps and has deprecated `apple-mobile-web-app-status-bar-style: black-translucent`, the old
way to (WebKit bugs 317153 and 301994), so `index.html` doesn't ask for it. What the page does choose is
that background: `<body>` is the rail's colour where the rail runs down that edge (`responsive.css`),
and `theme-color` follows it (`layout.ts`).

`web/src/layout.ts` turns that into one of five layouts and puts it on `<html data-layout>`;
`web/src/responsive.css` places the panes per layout, and holds the touch and narrow-screen rules.

- `wide` — a desktop: workspace | chat (draggable splitter) | rail.
- `dual` — a touch screen with room for both: they split down the middle.
- `book` / `laptop` — half-folded. The panes land on the two segments, so nothing sits under the
  hinge. Only Safari with "Viewport Segments API" switched on reports the fold; everything falls
  back to an even split, and the segment-exact sizes are guarded by the media query that proves
  the segments exist.
- `single` — too narrow for both (the outer screen, the inner one in portrait, Split View): the
  chat and the workspace take turns. Both stay mounted so neither loses its scroll or editors;
  picking an agent shows the chat, picking a panel shows the workspace. The rail is a drawer
  there: the last button of the bar on top (`<RailToggle />`, `components/Rail.tsx`) opens it, and
  going anywhere closes it, so a new bar that can be on top ends with one.

Apart from the layout, `<html data-compact>` marks a phone-sized touch screen (every pose above):
type, bars and the rail a size down (the block at the end of `responsive.css`). The chat's reading
size is `--fs` (12px there); size new chat text from it, not in px. The board is an iframe and has
its own narrow-width rules in `atrium/board/template.py`. Agents write what is in it, so nothing they
write may make it wider than its frame, which a finger then drags sideways: a code block, a table or
an image scrolls inside its card, a path with nowhere to break breaks at the edge, and a grid column
that holds their content is `minmax(0, 1fr)` (a `1fr` one is never narrower than what it holds).
`duo-check.py` adds content that wide to the board and reports one that scrolls sideways.

What to get right in a new component:

- **Nothing may depend on hover.** A finger has no hover, and on iOS a tap that reveals something
  is swallowed as a hover — the button then needs a second tap. Put hover-reveal CSS in
  `@media (hover: hover)` with something a finger can reach as well (the open row, an
  always-visible button). Tooltips: a plain one is `data-tip="…"` (`tips.ts` shows it on hover and
  on a finger's long press, and the press then doesn't click); a rich one is `useTip`
  (`components/ui.tsx`), with `{ tap: true }` on something that only shows information, like a
  read-only chip, so that a tap shows it. A native `title` never shows on iOS.
- **Icons, not words, for controls.** A segmented choice or a toolbar button is an icon with an
  `aria-label` and a `data-tip`; the words live in the tip. A panel's toolbar has to fit beside
  the chat on the inner screen, and alone on the outer one.
- **Drag with pointer events, never mouse ones.** iOS makes up no `mousemove` while a finger
  moves, so an `onMouseDown` + `window.mousemove` splitter is dead on a phone however big its
  handle is. Use `startRowResize` (`components/ui.tsx`) or the chat splitter's pattern in
  `App.tsx`, and give the handle `touch-action: none` or the page scrolls instead.
- **Tap targets ≥ 30px** under `@media (pointer: coarse)`, and the row around them has to grow
  too, or the button spills out of it. The one exception is a text button sitting inside a line
  of prose (`.sub-link.inline`), where a 30px box would break the line; it keeps its text size.
- **Text fields may be under 16px.** iOS zooms into such a field when it gets focus and stays
  zoomed; `index.html` adds `maximum-scale=1` on iOS only, which stops that while pinch zoom keeps
  working there. Don't add it (or `user-scalable=no`) for other platforms: Android takes it
  literally and blocks zooming.
- **Every page sets `-webkit-text-size-adjust: 100%` on `html`, an iframe's too** (`responsive.css`
  for the app, `atrium/board/template.py` for the board). Without it Safari enlarges the text of
  any block wider than the phone's screen, which on the Duo means most of the inner screen.
  `duo-check.py` can't see this: Playwright's WebKit is built without that inflation.
- **Use `dvh`, not `vh`**: on a phone `100vh` includes what Safari's bars cover.
- **The page reaches under the notch and the home indicator** (`viewport-fit=cover`, for the Home
  Screen app, which has no Safari bars): something fixed to an edge of the window adds
  `env(safe-area-inset-*)` (0 on a desktop). So do the panes: each runs its background out to the
  edges it meets, `--edge-top/right/bottom/left` (`responsive.css`; 0 on an edge it doesn't meet),
  and pads its content by the top and side insets. `.app` has no padding, or bare strips of page
  show round the panes. The bottom one is left to what the pane ends on: a bar (a folded drawer, the
  editor's status line) pads under its controls by `--edge-bottom`, and a list scrolls on under the
  home indicator with that much more room at its end. Padding the pane instead ends it on a blank
  strip, its lists cut off above it; a new component that can end a pane takes `--edge-bottom`
  itself. The composer is the exception, by the human's choice: it keeps just its own gap, the one at
  its sides, and sits on the inset's upper part (lifted clear of all of it, it floated over a blank
  strip). Something whose background differs from its pane's (the dark terminal) runs out to the side
  edges through the same variables.
- **Fullscreen through `useFullscreen`** (`components/ui.tsx`, the element gets `fs-host`).
  iPhone's Safari has no fullscreen for page elements, so there the element fills the window, and
  meets its edges as a pane does: the top and the sides padded by the insets, the bottom left to
  what it ends on (`--edge-bottom`; padded there, the review ended on a blank strip).
- **Style scrollbars for a mouse only** (`@media (hover: hover) and (pointer: fine)`). WebKit's
  mobile layout leaves a styled scrollbar no room, takes its width off `clientWidth` anyway, and
  every scroll box then scrolls sideways by it.
- **A label that must not be cut is not the flex child that grows.** `flex: 1; min-width: 0` on a
  label collapses it to nothing and its text then paints over its neighbour (what `.drawer-head`
  did). Let the long explanatory text shrink and ellipsis instead, and hide it outright on a
  narrow panel.
- Panels sit inside the `.workspace` container, so prefer `@container (max-width: …)` over
  `@media` for anything that should follow the panel rather than the window.

How to check it (no device or Xcode needed; WebKit is the engine Safari runs):

```sh
# an isolated instance on a scratch project, so nothing touches this repo's .atrium/ — and its own
# Claude directory, so the transcripts you write for it do not land in yours
export ATRIUM_HOME=$(mktemp -d) CLAUDE_CONFIG_DIR=$(mktemp -d)
.venv/bin/atrium --port 8841 --no-browser --no-auth <scratch dir>
uv run --with playwright==1.64.1 python -m playwright install webkit        # once
uv run --with playwright==1.64.1 python web/scripts/duo-check.py \
    --base http://127.0.0.1:8841 --shots /tmp/duo-shots
```

Give that scratch project a few agents and some uncommitted edits first, or the chat is the empty
new-agent box and Changes and Git are their empty states — almost nothing would be checked, and
`duo-check.py` exits 2 rather than hand you that green light. An agent is a transcript at
`$CLAUDE_CONFIG_DIR/projects/<key>/<uuid>.jsonl`, where `<key>` is the project's real path with
every character that is not a letter or a digit replaced by `-`; one user and one assistant entry
is enough to make a session, and a `toolUseResult` with `filePath` makes it show up in Changes.

`web/scripts/duo-check.py` drives every pose above as a touch device — the home page, the chat,
the rail's drawer where it is one, each workspace panel (the review also full screen), then back to
the chat through the drawer —
and fails on a pose that resolves to the wrong layout or misses the phone sizes (`data-compact`), a
drawer that doesn't lead back to the chat, boxes pushed past the viewport,
panels that scroll sideways, text painted outside its own box, labels squeezed to nothing, buttons
under 30px, buttons spilling out of their row, and bare page showing between the panes. It also
drives the Home Screen poses with their insets (Playwright's WebKit reports none, so it writes them
into the CSS), where a control under an inset is a finding too (in a list, scrolled to its end; the
composer aside), and so is a pane that stops a blank strip short of the bottom. It only sees what is
on screen, so a control that needs a particular state (an attachment in the composer, a forked
agent, a subagent's breadcrumb, an open menu, the editor or a commit in fullscreen) still wants a look of its own —
and no check of this kind can tell you something is unreadable. Read the screenshots.

## Keep this in sync

The Makefile is the source of truth. If you change how Atrium is installed, built or run,
update README "Quick start" and this file in the same change.

`docs/configuration.md` documents every setting, and `docs/configuration.zh-CN.md` is its Chinese
version. If you add, rename or re-default a key in `DEFAULT_SETTINGS`, or change what Atrium reads from
or sets over a harness's own files, update both in the same change.

The phone rules above and `web/scripts/duo-check.py` have to agree: the minimum tap target is
written in both (`MIN_TAP`). If you change one, change the other.
