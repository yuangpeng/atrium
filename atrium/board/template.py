"""Render the self-contained ``board.html``.

The page has no external dependencies so it can be opened straight from disk,
embedded in Atrium (sandboxed iframe), or shared as a file. Agent-authored card
HTML is inserted as-is; the stylesheet below is the "design system" agents are
told about in their system prompt (see ``prompts.py``).
"""

from __future__ import annotations

import html
import time
from typing import Any

STATUS_LABEL = {"working": "Working", "waiting": "Needs you", "blocked": "Blocked", "done": "Done", "idle": "Idle"}
LIVE_LABEL = {"running": "running", "waiting": "waiting for approval", "idle": "attached", "starting": "starting"}

CSS = r"""
:root{
  --bg:#FAF9F5; --bg2:#F5F4ED; --panel:#FFFFFF; --line:#E8E6DC; --line2:#DEDCD1;
  --text:#141413; --text2:#3D3D3A; --muted:#73726C; --faint:#A3A199;
  --accent:#D97757; --accent2:#C6613F; --accent-bg:#F9EDE7;
  --ok:#3F7D58; --ok-bg:#E7F1EA; --warn:#A86B12; --warn-bg:#FBF0DC; --err:#B5443A; --err-bg:#F8E5E2; --info:#3B6FA8; --info-bg:#E5EDF7;
  --serif:"Tiempos Headline","Iowan Old Style","Charter","Georgia",ui-serif,serif;
  --sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue","PingFang SC","Hiragino Sans GB",sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
}
[data-theme=dark]{
  --bg:#262624; --bg2:#1F1E1D; --panel:#30302E; --line:#3E3E3A; --line2:#4A4944;
  --text:#FAF9F5; --text2:#E5E4DF; --muted:#A6A39A; --faint:#78766F;
  --accent:#D97757; --accent2:#E08A6D; --accent-bg:#3D2E28;
  --ok:#7DBB91; --ok-bg:#25342A; --warn:#E0B060; --warn-bg:#3A3122; --err:#E8857A; --err-bg:#3D2724; --info:#86AEDD; --info-bg:#243142;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent2)}
.wrap{max-width:1180px;margin:0 auto;padding:28px 28px 64px}
header.top{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;padding-bottom:18px;border-bottom:1px solid var(--line)}
.eyebrow{font:600 11px/1 var(--sans);letter-spacing:.12em;text-transform:uppercase;color:var(--accent2);margin-bottom:10px}
h1{font:500 30px/1.15 var(--serif);margin:0;letter-spacing:-.01em}
.headline{font:400 17px/1.5 var(--serif);color:var(--text2);margin:10px 0 0;max-width:760px}
.meta{color:var(--muted);font-size:12.5px;text-align:right;white-space:nowrap}
.meta b{color:var(--text2);font-weight:600}
h2{font:500 19px/1.3 var(--serif);margin:34px 0 14px;display:flex;align-items:center;gap:10px}
h2 .count{font:500 12px var(--sans);color:var(--muted);background:var(--bg2);border:1px solid var(--line);border-radius:99px;padding:1px 8px}
.overview{margin-top:18px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 20px}
.overview:empty{display:none}
.overview ul{margin:4px 0;padding-left:20px}
.agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px 12px;display:flex;flex-direction:column;gap:8px;cursor:pointer;transition:border-color .15s, box-shadow .15s;position:relative}
.tile:hover{border-color:var(--line2);box-shadow:0 2px 10px rgba(0,0,0,.05)}
.tile .row{display:flex;align-items:center;gap:8px}
.tile .name{font:600 14.5px/1.3 var(--sans);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tile .doing{color:var(--text2);font-size:13.5px;min-height:20px}
.tile .summary{color:var(--muted);font-size:12.5px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.tile .foot{display:flex;gap:10px;flex-wrap:wrap;color:var(--faint);font-size:11.5px;margin-top:2px}
.tile .foot span{white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.st-working .dot{background:var(--accent);box-shadow:0 0 0 3px var(--accent-bg);animation:pulse 1.6s ease-in-out infinite}
.st-waiting .dot{background:var(--warn);box-shadow:0 0 0 3px var(--warn-bg)}
.st-blocked .dot{background:var(--err)}
.st-done .dot{background:var(--ok)}
@keyframes pulse{50%{opacity:.45}}
.pill{font:600 10.5px/1 var(--sans);letter-spacing:.04em;text-transform:uppercase;padding:4px 7px;border-radius:99px;background:var(--bg2);color:var(--muted);border:1px solid var(--line)}
.st-working .pill{background:var(--accent-bg);color:var(--accent2);border-color:transparent}
.st-waiting .pill{background:var(--warn-bg);color:var(--warn);border-color:transparent}
.st-blocked .pill{background:var(--err-bg);color:var(--err);border-color:transparent}
.st-done .pill{background:var(--ok-bg);color:var(--ok);border-color:transparent}
.progress{height:5px;background:var(--bg2);border-radius:99px;overflow:hidden;border:1px solid var(--line)}
.progress>span{display:block;height:100%;background:var(--accent);border-radius:99px;transition:width .4s}
.st-done .progress>span{background:var(--ok)}
.results{display:flex;flex-direction:column;gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.card>header{display:flex;align-items:center;gap:10px;padding:11px 18px;border-bottom:1px solid var(--line);background:var(--bg2)}
.card>header .who{font-weight:600;font-size:13px;cursor:pointer}
.card>header .title{font:500 15px var(--serif);flex:1;min-width:0}
.card>header .when{color:var(--faint);font-size:11.5px;white-space:nowrap}
.card-body{padding:16px 20px;overflow-x:auto}
.card-body>*:first-child{margin-top:0}.card-body>*:last-child{margin-bottom:0}
.card-body h1,.card-body h2,.card-body h3,.card-body h4{font-family:var(--serif);font-weight:500;margin:1.1em 0 .45em;display:block}
.card-body h1{font-size:21px}.card-body h2{font-size:18px}.card-body h3{font-size:15.5px}.card-body h4{font-size:14px}
.timeline{list-style:none;margin:0;padding:0;border-left:2px solid var(--line);margin-left:6px}
.timeline li{position:relative;padding:4px 0 10px 18px;font-size:13px;color:var(--text2)}
.timeline li::before{content:"";position:absolute;left:-6px;top:10px;width:10px;height:10px;border-radius:50%;background:var(--panel);border:2px solid var(--line2)}
.timeline li.k-tracker::before{border-color:var(--info)}
.timeline li.k-note::before{border-color:var(--accent)}
.timeline .who{font-weight:600;color:var(--text);cursor:pointer}
.timeline .when{color:var(--faint);font-size:11.5px;margin-left:6px}
.empty{color:var(--muted);background:var(--panel);border:1px dashed var(--line2);border-radius:14px;padding:26px;text-align:center}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:28px}
@media (max-width:900px){.cols{grid-template-columns:1fr}}
/* ---- design system for agent-authored cards ---- */
table{border-collapse:collapse;width:100%;font-size:13px;margin:.6em 0}
th,td{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;background:var(--bg2)}
code{font:12.5px var(--mono);background:var(--bg2);border:1px solid var(--line);border-radius:5px;padding:1px 5px}
pre{font:12.5px/1.5 var(--mono);background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow:auto}
pre code{background:none;border:0;padding:0}
details{border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:.6em 0}
summary{cursor:pointer;font-weight:600}
blockquote{margin:.6em 0;padding:2px 14px;border-left:3px solid var(--line2);color:var(--text2)}
.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:.6em 0}
.kpi{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.kpi .value{font:500 24px/1.1 var(--serif)}
.kpi .label{color:var(--muted);font-size:12px;margin-top:4px}
.badge{display:inline-block;font:600 11px/1 var(--sans);padding:4px 8px;border-radius:99px;background:var(--bg2);color:var(--text2);border:1px solid var(--line);vertical-align:middle}
.badge.ok{background:var(--ok-bg);color:var(--ok);border-color:transparent}
.badge.warn{background:var(--warn-bg);color:var(--warn);border-color:transparent}
.badge.err{background:var(--err-bg);color:var(--err);border-color:transparent}
.badge.info{background:var(--info-bg);color:var(--info);border-color:transparent}
.callout{border-radius:10px;padding:10px 14px;margin:.6em 0;background:var(--bg2);border:1px solid var(--line)}
.callout.ok{background:var(--ok-bg);border-color:transparent}
.callout.warn{background:var(--warn-bg);border-color:transparent}
.callout.err{background:var(--err-bg);border-color:transparent}
.callout.info{background:var(--info-bg);border-color:transparent}
ul.files{list-style:none;padding:0;margin:.5em 0}
ul.files li{font:12.5px var(--mono);padding:3px 0;border-bottom:1px dashed var(--line)}
.add{color:var(--ok)}.del{color:var(--err)}
"""

JS = r"""
(function(){
  var q = new URLSearchParams(location.search), theme = q.get('theme');
  if(!theme || theme==='system'){ theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
  document.documentElement.setAttribute('data-theme', theme);
  function rel(ts){ var s=(Date.now()/1000-ts); if(s<45) return 'just now'; if(s<3600) return Math.round(s/60)+'m ago';
    if(s<86400) return Math.round(s/3600)+'h ago'; return Math.round(s/86400)+'d ago'; }
  function tick(){ document.querySelectorAll('[data-ts]').forEach(function(el){ var t=+el.getAttribute('data-ts'); if(t) el.textContent=rel(t); }); }
  tick(); setInterval(tick, 30000);
  document.addEventListener('click', function(e){
    var el = e.target.closest('[data-session]'); if(!el || e.target.closest('a,summary,button')) return;
    if(window.parent !== window) window.parent.postMessage({type:'atrium:open-session', sessionId: el.getAttribute('data-session')}, '*');
  });
  // Embedded in Atrium: the host pushes fresh HTML and we swap the content in place (keeps scroll).
  window.addEventListener('message', function(e){
    var d = e.data || {};
    if(d.type === 'atrium:board-html' && d.html){
      var doc = new DOMParser().parseFromString(d.html, 'text/html');
      var next = doc.querySelector('.wrap'), cur = document.querySelector('.wrap');
      if(next && cur){ cur.innerHTML = next.innerHTML; tick(); }
    }
    if(d.type === 'atrium:theme' && d.theme){ document.documentElement.setAttribute('data-theme', d.theme); }
  });
  if(window.top === window){ setTimeout(function(){ location.reload(); }, 20000); }
})();
"""


def _e(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def _ts(t: float | None) -> str:
    if not t:
        return ""
    return f'<span data-ts="{t:.0f}">{time.strftime("%m-%d %H:%M", time.localtime(t))}</span>'


def _agent_order(a: dict[str, Any]) -> tuple[int, float]:
    rank = {"waiting": 0, "blocked": 1, "working": 2, "done": 3, "idle": 4}.get(a.get("status", "idle"), 5)
    live = (a.get("stats") or {}).get("live")
    if live in ("running", "waiting"):
        rank = min(rank, 0 if live == "waiting" else 2)
    return (rank, -(a.get("updated_at") or 0))


def _tile(a: dict[str, Any]) -> str:
    st = a.get("status") or "idle"
    stats = a.get("stats") or {}
    live = stats.get("live")
    if live == "waiting":
        st = "waiting"
    elif live == "running" and st in ("idle", "done"):
        st = "working"
    foot = []
    if live:
        foot.append(f"<span>● {_e(LIVE_LABEL.get(live, live))}</span>")
    if stats.get("harness") and stats["harness"] != "claude-code":
        foot.append(f"<span>◆ {_e(stats['harness'])}</span>")
    if stats.get("branch"):
        foot.append(f"<span>⎇ {_e(stats['branch'])}{' (worktree)' if stats.get('worktree') else ''}</span>")
    if stats.get("files_changed"):
        n = int(stats["files_changed"])
        foot.append(f"<span>{n} file{'' if n == 1 else 's'} changed</span>")
    last = stats.get("last_active") or a.get("updated_at")
    if last:
        foot.append(f"<span>{_ts(last)}</span>")
    prog = int(a.get("progress") or 0)
    return f"""
<div class="tile st-{_e(st)}" data-session="{_e(a.get('session_id'))}">
  <div class="row"><span class="dot"></span><span class="name" title="{_e(a.get('name'))}">{_e(a.get('name') or 'Agent')}</span><span class="pill">{_e(STATUS_LABEL.get(st, st))}</span></div>
  <div class="progress" title="{prog}%"><span style="width:{prog}%"></span></div>
  <div class="doing">{_e(a.get('doing')) or '<span class="muted">—</span>'}</div>
  {f'<div class="summary">{_e(a.get("summary"))}</div>' if a.get('summary') else ''}
  <div class="foot">{''.join(foot)}</div>
</div>"""


def render_board(state: dict[str, Any], project_name: str, project_path: str) -> str:
    agents = [a for a in state.get("agents", {}).values() if not (a.get("stats") or {}).get("archived")]
    agents.sort(key=_agent_order)
    active = sum(1 for a in agents if a.get("status") in ("working", "waiting", "blocked") or (a.get("stats") or {}).get("live") in ("running", "waiting"))
    ov = state.get("overview") or {}

    tiles = "".join(_tile(a) for a in agents) or '<div class="empty">No agents yet. Start one from Atrium — it will appear here.</div>'

    cards = []
    for a in agents:
        for key, c in (a.get("cards") or {}).items():
            cards.append((c.get("updated_at") or 0, a, key, c))
    cards.sort(key=lambda x: -x[0])
    card_html = "".join(
        f"""
<article class="card" id="card-{_e(a.get('session_id'))}-{_e(key)}">
  <header><span class="who" data-session="{_e(a.get('session_id'))}">{_e(a.get('name'))}</span><span class="title">{_e(c.get('title'))}</span><span class="when">{_ts(c.get('updated_at'))}</span></header>
  <div class="card-body">{c.get('html') or ''}</div>
</article>"""
        for _, a, key, c in cards
    ) or '<div class="empty">Agents publish their results here as HTML cards.</div>'

    tl = list(reversed(state.get("timeline") or []))[:60]
    tl_html = "".join(
        f"""<li class="k-{_e(t.get('kind'))}">{f'<span class="who" data-session="{_e(t.get("session_id"))}">{_e(t.get("agent"))}</span> ' if t.get('agent') else ''}{_e(t.get('text'))}<span class="when">{_ts(t.get('ts'))}</span></li>"""
        for t in tl
    ) or '<li class="muted">Nothing yet.</li>'

    updated = state.get("updated_at") or time.time()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(project_name)} · Atrium Board</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header class="top">
  <div>
    <div class="eyebrow">Atrium Board</div>
    <h1>{_e(project_name)}</h1>
    {f'<p class="headline">{_e(ov.get("headline"))}</p>' if ov.get('headline') else ''}
  </div>
  <div class="meta"><b>{len(agents)}</b> agents · <b>{active}</b> active<br>updated {_ts(updated)}<br><span title="{_e(project_path)}">{_e(project_path)}</span></div>
</header>
<section class="overview">{ov.get('html') or ''}</section>
<h2>Agents <span class="count">{len(agents)}</span></h2>
<section class="agents">{tiles}</section>
<div class="cols">
  <div><h2>Results <span class="count">{len(cards)}</span></h2><section class="results">{card_html}</section></div>
  <div><h2>Timeline</h2><ul class="timeline">{tl_html}</ul></div>
</div>
</div>
<script>{JS}</script>
</body></html>
"""
