#!/usr/bin/env python3
"""Check Atrium's UI at iPhone Duo's screen sizes. See AGENTS.md, "Every UI change has to work
on a phone, iPhone Duo included".

It drives Safari's engine (Playwright's WebKit) at each pose, as a touch device — the home page,
the chat, the rail's drawer where the screen is too narrow for a docked one, then each workspace
panel, and back to the chat through the drawer — and walks the DOM for the mistakes that are easy to make and hard to see: boxes pushed
past the viewport, text painted outside its own box, boxes squeezed to nothing (a label with
`flex: 1; min-width: 0` collapses and paints over its neighbour), and buttons too small for a
fingertip. Every pose also has to get the phone sizes (<html data-compact>, layout.ts).

    # an isolated instance on a scratch project, so nothing touches a real .atrium/
    ATRIUM_HOME=$(mktemp -d) .venv/bin/atrium --port 8841 --no-browser --no-auth /tmp/scratch
    uv run --with playwright==1.64.1 python -m playwright install webkit   # once
    uv run --with playwright==1.64.1 python web/scripts/duo-check.py --base http://127.0.0.1:8841

Exits non-zero when it finds something. `--shots <dir>` also writes a screenshot per pose and
panel; look at those too, since a check like this cannot see "ugly" or "unreadable".
"""
import argparse
import asyncio
import json
import re
import sys
import urllib.request

# Safari 27.1's own table (WebKit, "WebKit Features for Safari 27.1"), at SVH: the browser's bars
# showing. The third value is what layout.ts has to resolve that pose to (<html data-layout>).
POSES = {
    "outer-portrait": (382, 580, "single"),
    "outer-landscape": (594, 368, "single"),
    "inner-portrait": (669, 867, "single"),
    "inner-landscape": (951, 585, "dual"),
    "split-view": (385, 571, "single"),
}
# Added to the Home Screen, Atrium opens as an app with no Safari bars: the whole screen, and the insets the device
# reports for it (top, right, bottom, left), measured from iPhone Duo's own screenshots. Its clock and Wi-Fi run down
# the right edge, the home indicator along the bottom. Playwright's WebKit reports no insets, so the check writes
# these into the CSS's env(safe-area-inset-*) as it loads.
APP_POSES = {
    "app-outer-portrait": (466, 678, "single", (0, 84, 34, 0)),
    "app-inner-landscape": (951, 669, "dual", (0, 84, 34, 0)),
}
MIN_TAP = 30  # keep this the same as the rule in AGENTS.md

# Monaco is virtualized and legitimately draws outside its bounds; xterm.js keeps its input textarea
# and its measuring element off-screen; the chips' measuring copy is hidden on purpose. Inline text
# buttons sit in a line of prose, where a 30px box would break it.
AUDIT = """([MIN_TAP, inset]) => {
  const out = [];
  const name = (el) => el.tagName.toLowerCase() + (typeof el.className === "string" && el.className.trim() ? "." + el.className.trim().split(/\\s+/).slice(0, 3).join(".") : "");
  const clipped = (el) => {
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const c = getComputedStyle(p);
      if (c.overflowX !== "visible" || c.overflow !== "visible") return true;
    }
    return false;
  };
  // what is left of a box once the boxes it scrolls in have cut it to their own
  const shown = (el, r) => {
    let { left, top, right, bottom } = r;
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const c = getComputedStyle(p);
      if (c.overflowX === "visible" && c.overflowY === "visible") continue;
      const q = p.getBoundingClientRect();
      left = Math.max(left, q.left); top = Math.max(top, q.top); right = Math.min(right, q.right); bottom = Math.min(bottom, q.bottom);
    }
    return right > left && bottom > top ? { left, top, right, bottom } : null;
  };
  const [it, ir, ib, il] = inset || [0, 0, 0, 0];
  for (const el of document.querySelectorAll("body *")) {
    if (el.closest(".monaco-editor, .monaco-aria-container, .xterm, .chip.measure, .sub-link.inline")) continue;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    const r = el.getBoundingClientRect();
    if (!r.height) continue;
    const leaf = el.children.length === 0 && (el.textContent || "").trim();
    if (!r.width) {
      // clipped (.ellipsis) is a label that quietly gives up its room; visible overflow is the bad
      // one, where the text carries on painting over whatever sits next to it
      if (leaf && cs.overflow === "visible")
        out.push({ kind: "box squeezed to nothing", el: name(el), text: (el.textContent || "").trim().slice(0, 60) });
      continue;
    }
    // a box that scrolls sideways hides content behind a gesture nobody makes on a phone. Code and
    // tables are meant to scroll that way; a panel or a list is not.
    if (el.scrollWidth > el.clientWidth + 1 && /auto|scroll/.test(cs.overflowX) && r.width > 120 &&
        !el.matches("pre, table, .code-block, .mini-diff, .editor-tabs, .md, .card-body, .msg-output"))
      out.push({ kind: "panel scrolls sideways", el: name(el), by: el.scrollWidth - el.clientWidth, width: Math.round(r.width) });
    if (leaf && el.scrollWidth > el.clientWidth + 1 && cs.textOverflow !== "ellipsis" && cs.overflow === "visible")
      out.push({ kind: "text outside its box", el: name(el), by: el.scrollWidth - el.clientWidth, text: (el.textContent || "").trim().slice(0, 60) });
    if (!clipped(el) && (r.right > innerWidth + 1 || r.left < -1))
      out.push({ kind: "past the viewport", el: name(el), right: Math.round(r.right), width: innerWidth });
    // under an inset, a control sits beneath the clock, the camera or the home indicator
    if (inset && el.matches("button, [role=button], input, textarea, select, a[href]")) {
      const v = shown(el, r);
      if (v && (v.top < it - 1 || v.right > innerWidth - ir + 1 || v.bottom > innerHeight - ib + 1 || v.left < il - 1))
        out.push({ kind: "control under an inset", el: name(el), box: [v.left, v.top, v.right, v.bottom].map(Math.round),
                   text: (el.textContent || el.getAttribute("aria-label") || el.title || "").trim().slice(0, 40) });
    }
    const button = el.tagName === "BUTTON" || el.getAttribute("role") === "button";
    if (button && !el.disabled) {
      if (r.width < MIN_TAP - 1 || r.height < MIN_TAP - 1)
        out.push({ kind: "button too small to tap", el: name(el), size: `${Math.round(r.width)}x${Math.round(r.height)}`,
                   text: (el.textContent || el.getAttribute("aria-label") || el.title || "").trim().slice(0, 40) });
      // growing a button for a fingertip only works if its row grew with it. A parent that scrolls
      // is not a finding: its children are meant to go past the edge.
      const par = el.parentElement, pr = par && par.getBoundingClientRect();
      const scrolls = par && (par.scrollHeight > par.clientHeight + 1 || /auto|scroll/.test(getComputedStyle(par).overflowY));
      if (pr && pr.height && !scrolls && (r.bottom > pr.bottom + 1 || r.top < pr.top - 1) && getComputedStyle(par).overflow !== "visible")
        out.push({ kind: "button spills out of its row", el: name(el), size: `${Math.round(r.height)} in ${Math.round(pr.height)}`,
                   text: (el.textContent || el.getAttribute("aria-label") || el.title || "").trim().slice(0, 40) });
    }
  }
  // the panes cover the window between them: where the page itself shows, one stopped short of an edge. Under an
  // inset a pane runs its background out to the edge and keeps only what it draws clear of it.
  const xs = [], ys = [];
  for (let x = 1; x < innerWidth - 1; x += 16) xs.push(x);
  for (let y = 1; y < innerHeight - 1; y += 16) ys.push(y);
  xs.push(innerWidth - 1); ys.push(innerHeight - 1);
  const bare = ys.map((y) => xs.map((x) => { const hit = document.elementFromPoint(x, y); return !hit || hit.matches("html, body, #root, .app"); }));
  // reported as the strips it makes (columns or rows bare from end to end), then what is left
  const cols = xs.map((_, i) => bare.every((row) => row[i])), rows = bare.map((row) => row.every(Boolean));
  const runs = (flags, at) => {
    const got = [];
    for (let i = 0, s = -1; i <= flags.length; i++)
      if (flags[i] && s < 0) s = i;
      else if (!flags[i] && s >= 0) (got.push(`${at[s]}-${at[i - 1]}`), (s = -1));
    return got;
  };
  for (const x of runs(cols, xs)) out.push({ kind: "bare page", el: "a strip top to bottom", x });
  for (const y of runs(rows, ys)) out.push({ kind: "bare page", el: "a strip side to side", y });
  const rest = bare.flatMap((row, j) => row.flatMap((b, i) => (b && !cols[i] && !rows[j] ? [[xs[i], ys[j]]] : [])));
  if (rest.length) {
    const span = (k) => `${Math.min(...rest.map((p) => p[k]))}-${Math.max(...rest.map((p) => p[k]))}`;
    out.push({ kind: "bare page", el: "patches", x: span(0), y: span(1), points: rest.length });
  }
  const seen = new Set();
  return out.filter((o) => { const k = JSON.stringify(o); if (seen.has(k)) return false; seen.add(k); return true; });
}"""


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via $http_proxy: this is localhost


def get(url: str):
    with _direct.open(url) as r:
        return json.load(r)


def with_insets(inset):
    """A route handler serving stylesheets as a device with these insets (top, right, bottom, left) has them:
    each env(safe-area-inset-*) a length."""
    px = dict(zip(("top", "right", "bottom", "left"), inset))

    async def handle(route):
        resp = await route.fetch()
        css = re.sub(r"env\(\s*safe-area-inset-(top|right|bottom|left)\s*(?:,[^)]*)?\)", lambda m: f"{px[m[1]]}px", await resp.text())
        await route.fulfill(response=resp, body=css)

    return handle


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8841", help="an Atrium started with --no-auth")
    ap.add_argument("--shots", help="directory for screenshots")
    ap.add_argument("--pose", action="append", choices=[*POSES, *APP_POSES], help="only this pose (repeatable)")
    args = ap.parse_args()
    from playwright.async_api import async_playwright

    projects = get(f"{args.base}/api/projects")
    if not projects:
        print("no project in that instance: start it with a scratch directory", file=sys.stderr)
        return 2
    pid = projects[0]["id"]
    sessions = [s for s in get(f"{args.base}/api/p/{pid}/sessions") if s.get("role") == "worker"]
    if not sessions:
        print(f"{projects[0]['path']} has no agent: the chat would be the empty new-agent box, and Changes and Git\n"
              "their empty states — almost nothing would be checked. Give the scratch project a few sessions\n"
              "(a transcript each) and some uncommitted edits first, then run this again.", file=sys.stderr)
        return 2
    sid = sessions[0]["id"]
    every = {**POSES, **APP_POSES}
    poses = {k: every[k] for k in (args.pose or every)}

    findings, errors = [], []
    if args.shots:
        __import__("os").makedirs(args.shots, exist_ok=True)

    async def audit(page, pose: str, label: str, inset=None):
        doc = await page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
        if doc[0] > doc[1] + 1:
            findings.append(f"{pose:16} {label:9} {'the page scrolls sideways':26} {doc[0]}px of content in {doc[1]}px")
        for o in await page.evaluate(AUDIT, [MIN_TAP, inset]):
            rest = json.dumps({k: v for k, v in o.items() if k not in ("kind", "el")})
            findings.append(f"{pose:16} {label:9} {o['kind']:26} {o['el'][:44]:44} {rest[:100]}")
        if args.shots:
            await page.screenshot(path=f"{args.shots}/{pose}-{label}.png")
    async with async_playwright() as pw:
        browser = await pw.webkit.launch()
        for pose, (w, h, want, *inset) in poses.items():
            inset = inset[0] if inset else None
            ctx = await browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2, is_mobile=True, has_touch=True)
            page = await ctx.new_page()
            if inset:
                await page.route("**/*.css", with_insets(inset))
            page.on("pageerror", lambda e, p=pose: errors.append(f"{p}: {e}"))
            # the home page first: it is what Atrium opens on when the URL names no project
            await page.goto(args.base + "/")
            await page.wait_for_timeout(1500)
            got, compact = await page.evaluate("() => [document.documentElement.dataset.layout, document.documentElement.hasAttribute('data-compact')]")
            if got != want:
                findings.append(f"{pose:16} {'(layout)':9} {'wrong layout':26} layout.ts resolved {w}x{h} to {got}, not {want}")
            if not compact:
                findings.append(f"{pose:16} {'(layout)':9} {'desktop sizes':26} layout.ts gave {w}x{h} no data-compact: a phone gets a desktop's type and bars")
            await audit(page, pose, "home", inset)
            await page.goto(f"{args.base}/?project={pid}&session={sid}")
            await page.wait_for_timeout(1500)
            await audit(page, pose, "chat", inset)
            drawer = page.locator(".rail-toggle:visible")  # one screen: the rail is a drawer, opened from the top bar
            if await drawer.count():
                await drawer.first.tap()
                await page.wait_for_timeout(400)
                await audit(page, pose, "rail", inset)
                await page.locator(".rail-backdrop").tap()
                await page.wait_for_timeout(400)
            back = page.locator('button[title="Board, files, changes, git, terminal"]')
            if await back.count():  # one screen: the chat covers the workspace
                await back.tap()
                await page.wait_for_timeout(400)
            for tab in ("Board", "Files", "Changes", "Git", "Terminal"):
                await page.locator(f'.ws-tabs button[aria-label="{tab}"]').tap()
                await page.wait_for_timeout(1300)
                await audit(page, pose, tab.lower(), inset)
                if tab == "Git":  # its second view: the worktrees and the agents in each
                    await page.locator('.toolbar button[aria-label="Worktrees"]').tap()
                    await page.wait_for_timeout(600)
                    await audit(page, pose, "worktrees", inset)
                    await page.locator('.toolbar button[aria-label="Graph"]').tap()
            # the way back from a panel: the open agent, picked in the drawer, brings its chat up and the drawer goes
            if await drawer.count():
                await drawer.first.tap()
                await page.wait_for_timeout(400)
                await page.locator(".rail-agent.on").tap()
                await page.wait_for_timeout(400)
                if not await page.evaluate("() => { const app = document.querySelector('.app'); return app.dataset.pane === 'chat' && !app.dataset.rail }"):
                    findings.append(f"{pose:16} {'rail':9} {'dead end':26} the open agent, picked in the drawer from a panel, didn't bring its chat back")
            print(f"{pose:16} {got or '?':8} checked", file=sys.stderr)
            await ctx.close()
        await browser.close()

    for line in sorted(set(findings)):
        print(line)
    for e in sorted(set(errors)):
        print(f"page error: {e}")
    if findings or errors:
        print(f"\n{len(set(findings))} finding(s), {len(set(errors))} page error(s)")
        return 1
    print("nothing to report", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
