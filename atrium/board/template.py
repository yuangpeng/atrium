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
/* else Safari enlarges the text of a block wider than the phone's screen: most of the board on the iPhone Duo's inner
   screen, or in landscape. The app's own rule (web/src/responsive.css) doesn't reach into this iframe. */
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
/* a path or a URL with nowhere to break (an agent is named after its first prompt) breaks at the edge it meets, not
   runs past it and widens the board */
body{overflow-wrap:break-word}
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
.overview{margin-top:18px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 20px;overflow-x:auto} /* a wide table scrolls in it, as in a card */
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
.timeline li.k-todo::before{border-color:var(--ok)}
.timeline .who{font-weight:600;color:var(--text);cursor:pointer}
.timeline .when{color:var(--faint);font-size:11.5px;margin-left:6px}
.empty{color:var(--muted);background:var(--panel);border:1px dashed var(--line2);border-radius:14px;padding:26px;text-align:center}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 300px;grid-template-areas:"results todo" "results timeline";grid-template-rows:auto 1fr;gap:0 28px;align-items:start}
/* minmax(0,…), not 1fr: a 1fr column is never narrower than its widest card's code block, table or image, and that
   made the whole board wider than a phone; this way those scroll inside their card (.card-body) */
.c-results{grid-area:results}.c-todo{grid-area:todo}.c-timeline{grid-area:timeline}
/* one column: the TODO first, not under every result */
@media (max-width:900px){.cols{grid-template-columns:minmax(0,1fr);grid-template-areas:"todo" "results" "timeline";grid-template-rows:none}}
/* ---- TODO: the human's own, then what agents suggest. Only the board in Atrium (html.embedded) edits it: it asks the
   host, which holds the login; opened on its own the list is read-only ---- */
.todo-add{display:flex;gap:6px;margin:0 0 10px}
.todo-add input{flex:1;min-width:0;font:inherit;font-size:13px;color:var(--text);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:6px 10px;outline:none}
.todo-add input:focus{border-color:var(--accent)}
.todo-add input::placeholder{color:var(--faint)}
.todo-add button{flex:none;width:32px;border-radius:10px;border:1px solid var(--line);background:var(--bg2);color:var(--text2);font:400 18px/1 var(--sans);cursor:pointer}
.todo-add button:hover{border-color:var(--line2);color:var(--text)}
.todos{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.todo{display:flex;align-items:flex-start;gap:4px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:4px 4px 4px 2px}
.todo-main{flex:1;min-width:0;padding:3px 0}
.todo-text{font-size:13px;line-height:1.45}
.todo-why{font-size:12px;color:var(--muted);margin-top:2px}
.todo-meta{font-size:11.5px;color:var(--faint);margin-top:2px}
.todo-meta .who{color:var(--muted);font-weight:600;cursor:pointer}
.todo button{flex:none;border:0;background:none;padding:0;cursor:pointer;width:26px;height:26px;border-radius:7px;display:grid;place-items:center;color:var(--faint);font:400 16px/1 var(--sans)}
.todo-box::before{content:"";width:15px;height:15px;border:1.5px solid var(--line2);border-radius:5px;display:grid;place-items:center;font:700 11px/1 var(--sans);color:var(--panel)}
.todo-box:hover::before{border-color:var(--accent)}
.todo.done .todo-box::before{content:"✓";background:var(--ok);border-color:var(--ok)}
.todo.done .todo-text{text-decoration:line-through;color:var(--muted)}
.todo-acts{display:flex;flex:none}
.todo-acts button:hover{background:var(--bg2);color:var(--text)}
.todo-acts .accept{color:var(--ok);font-weight:700;font-size:14px}
.todos.sug .todo{border-style:dashed;background:transparent;padding-left:10px} /* tentative until accepted */
.todo-group{display:flex;align-items:center;gap:8px;margin:14px 0 8px;font:600 11px/1 var(--sans);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.todo-group .count,.todo-done summary .count{font:500 11px var(--sans);letter-spacing:0;text-transform:none;color:var(--muted);background:var(--bg2);border:1px solid var(--line);border-radius:99px;padding:1px 7px}
details.todo-done{border:0;border-radius:0;padding:0;margin:14px 0 0}
.todo-done summary{display:flex;align-items:center;gap:8px;font:600 11px/1 var(--sans);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.todo-empty{color:var(--muted);font-size:12.5px;margin:2px 0}
.todo.pending{opacity:.5;pointer-events:none}
html:not(.embedded) :is(.todo-add,.todo-acts){display:none}
html:not(.embedded) .todo-box{pointer-events:none}
/* a mouse brings the delete button with it; a finger has it showing */
@media (hover:hover){.todos:not(.sug) .todo-acts{opacity:0}.todo:hover .todo-acts,.todo:focus-within .todo-acts{opacity:1}}
@media (pointer:coarse){.todo button{width:30px;height:30px}.todo-add button{width:36px}.todo-add input{min-height:34px}}
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
/* ---- a narrow board: a phone, or a panel beside the chat. Type a size down (the app's is set by --fs in
   web/src/responsive.css), the header stacked so the headline gets the width ---- */
@media (max-width:700px){
html,body{font-size:12px}
.wrap{padding:16px 14px 40px}
header.top{flex-direction:column;align-items:flex-start;gap:8px;padding-bottom:12px}
.meta{text-align:left;white-space:normal;font-size:11px}
.eyebrow{font-size:10px;margin-bottom:6px}
h1{font-size:20px}
.headline{font-size:13px;margin-top:6px}
h2{font-size:15px;margin:22px 0 10px}
.overview{padding:10px 14px;margin-top:12px}
.agents{grid-template-columns:minmax(0,1fr);gap:10px} /* 0: a long name ellipsizes, not widens every tile past the screen */
.tile{padding:10px 12px;gap:6px}
.tile .name{font-size:13px}.tile .doing{font-size:12px}.tile .summary{font-size:11.5px}
.card>header{padding:8px 12px}.card>header .title{font-size:13.5px}
.card-body{padding:12px 14px}
.card-body h1{font-size:17px}.card-body h2{font-size:15px}.card-body h3{font-size:13.5px}.card-body h4{font-size:12.5px}
table{font-size:11.5px}code,pre{font-size:11px}
.kpi .value{font-size:19px}
.timeline li{font-size:12px}
.cols{column-gap:18px}
.todo-text,.todo-add input{font-size:12px}.todo-why{font-size:11.5px}.todo-meta{font-size:11px}
}
"""

JS = r"""
(function(){
  var q = new URLSearchParams(location.search), theme = q.get('theme');
  if(!theme || theme==='system'){ theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
  document.documentElement.setAttribute('data-theme', theme);
  function rel(ts){ var s=(Date.now()/1000-ts); if(s<45) return 'just now'; if(s<3600) return Math.round(s/60)+'m ago';
    if(s<86400) return Math.round(s/3600)+'h ago'; return Math.round(s/86400)+'d ago'; }
  function tick(){ document.querySelectorAll('[data-ts]').forEach(function(el){ var t=+el.getAttribute('data-ts'); if(t) el.textContent=rel(t); }); }
  var embedded = window.parent !== window;
  if(embedded) document.documentElement.classList.add('embedded');
  // What the server last sent: each region's HTML (a region that comes again the same is left as it is, its open
  // <details> too, and nothing is laid out again), and what is round them. Read before tick() rewrites the times, and
  // kept as it came: the page itself may differ from it (the browser's own attributes on a field being typed in).
  var last, skeleton;
  function remember(wrap){
    last = {};
    wrap.querySelectorAll('[data-region]').forEach(function(r){ last[r.getAttribute('data-region')] = r.innerHTML; });
    skeleton = emptied(wrap);
  }
  function emptied(wrap){
    var c = wrap.cloneNode(true);
    c.querySelectorAll('[data-region]').forEach(function(r){ r.innerHTML = ''; });
    return c.innerHTML;
  }
  remember(document.querySelector('.wrap'));
  tick(); setInterval(tick, 30000);
  // the TODO list is changed by the host (Atrium), which holds the login; the board it pushes back shows the result
  function todo(msg){ msg.type = 'atrium:todo'; window.parent.postMessage(msg, '*'); }
  function addTodo(){
    var i = document.getElementById('todo-input'), t = i && i.value.trim();
    if(t){ todo({action:'add', text:t}); i.value = ''; }
  }
  // no <form>: a sandbox without allow-forms drops a form's submit before its submit event
  document.addEventListener('keydown', function(e){
    if(e.target.id === 'todo-input' && e.key === 'Enter' && !e.isComposing && e.keyCode !== 229){ e.preventDefault(); addTodo(); }
  });
  document.addEventListener('click', function(e){
    var act = e.target.closest('[data-act]');
    if(act && embedded){
      var a = act.getAttribute('data-act'), li = act.closest('[data-todo]');
      if(a === 'add') addTodo();
      else if(li){ li.classList.add('pending'); todo({action:a, id:li.getAttribute('data-todo')}); }
      return;
    }
    var el = e.target.closest('[data-session]'); if(!el || e.target.closest('a,summary,button')) return;
    if(embedded) window.parent.postMessage({type:'atrium:open-session', sessionId: el.getAttribute('data-session')}, '*');
  });
  // Embedded in Atrium: the host pushes fresh HTML and we swap the content in place (keeps scroll).
  window.addEventListener('message', function(e){
    var d = e.data || {};
    if(d.type === 'atrium:board-html' && d.html){
      var doc = new DOMParser().parseFromString(d.html, 'text/html');
      // From a newer Atrium than this page (the host pushes right after a restart): its stylesheet comes in too, or an
      // open board keeps the old layout, the Home Screen app's until it is closed. A new script can't come in that way:
      // the page loads again, at the theme it shows.
      var js = doc.getElementById('board-js'), css = doc.getElementById('board-css'), own = document.getElementById('board-css');
      if(js && js.textContent !== document.getElementById('board-js').textContent){
        location.replace(location.pathname + '?theme=' + document.documentElement.getAttribute('data-theme'));
        return;
      }
      if(css && css.textContent !== own.textContent) own.textContent = css.textContent;
      var next = doc.querySelector('.wrap'), cur = document.querySelector('.wrap');
      if(!next || !cur) return;
      if(emptied(next) === skeleton){
        // region by region: what is between them, the TODO's input, stays, with what is being typed in it and the
        // phone's keyboard up
        var open = {};
        cur.querySelectorAll('details[id]').forEach(function(d){ open[d.id] = d.open; });
        next.querySelectorAll('[data-region]').forEach(function(r){
          var name = r.getAttribute('data-region'), mine = cur.querySelector('[data-region="' + name + '"]');
          if(mine && (r.innerHTML !== last[name] || mine.querySelector('.pending'))) mine.innerHTML = r.innerHTML;
        });
        cur.querySelectorAll('details[id]').forEach(function(d){ if(d.id in open) d.open = open[d.id]; });
      } else {  // laid out differently (a new Atrium): all of it, but for the field being typed in
        var field = document.getElementById('todo-input'), typing = document.activeElement === field;
        cur.innerHTML = next.innerHTML;
        var fresh = document.getElementById('todo-input');
        if(field && fresh){ fresh.replaceWith(field); if(typing) field.focus(); }
      }
      remember(next);
      tick();
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
    if stats.get("branch") and stats.get("workspace_gone"):  # its worktree was removed; it continues once moved
        foot.append(f"<span>⎇ <s>{_e(stats['branch'])}</s> ({'worktree ' if stats.get('worktree') else ''}removed)</span>")
    elif stats.get("branch"):
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


def is_suggestion(t: dict[str, Any]) -> bool:
    """An agent's suggestion the human hasn't accepted (nor has anyone done it)."""
    return t.get("source") == "agent" and not t.get("accepted") and not t.get("done")


def _who(t: dict[str, Any]) -> str:
    return f'<span class="who" data-session="{_e(t.get("session_id"))}">{_e(t.get("agent") or "Agent")}</span>' if t.get("session_id") else _e(t.get("agent"))


def _todo(t: dict[str, Any]) -> str:
    sug, done = is_suggestion(t), bool(t.get("done"))
    if done:
        meta = f"done by {_e(t['done_by'])} · " if t.get("done_by") else "done "
        meta += _ts(t.get("done_at"))
    else:
        meta = ("" if sug else "from " if t.get("source") == "agent" else "via ") + _who(t) + " · " if t.get("agent") else ""
        meta += _ts(t.get("created_at"))
    box = "" if sug else (f'<button class="todo-box" data-act="{"reopen" if done else "done"}" aria-label="{"Not done" if done else "Done"}" '
                          f'title="{"Not done" if done else "Done"}"></button>')
    acts = ('<button class="accept" data-act="accept" aria-label="Add to my TODOs" title="Add to my TODOs">✓</button>'
            '<button data-act="remove" aria-label="Dismiss" title="Dismiss">×</button>') if sug else \
        '<button data-act="remove" aria-label="Delete" title="Delete">×</button>'
    return f"""<li class="todo{' done' if done else ''}" data-todo="{_e(t.get('id'))}">{box}<div class="todo-main">
<div class="todo-text">{_e(t.get('text'))}</div>{f'<div class="todo-why">{_e(t.get("why"))}</div>' if t.get('why') and not done else ''}
<div class="todo-meta">{meta}</div></div><span class="todo-acts">{acts}</span></li>"""


def _todos(todos: list[dict[str, Any]]) -> tuple[int, str]:
    """How many of the human's are open, and the list: theirs, agents' suggestions, done (folded)."""
    mine = sorted((t for t in todos if not t.get("done") and not is_suggestion(t)), key=lambda t: -(t.get("created_at") or 0))
    sug = sorted((t for t in todos if is_suggestion(t)), key=lambda t: -(t.get("created_at") or 0))
    done = sorted((t for t in todos if t.get("done")), key=lambda t: -(t.get("done_at") or 0))
    out = f'<ul class="todos">{"".join(map(_todo, mine))}</ul>' if mine else '<p class="todo-empty">Nothing open.</p>'
    if sug:
        out += f'<div class="todo-group">Suggested by agents <span class="count">{len(sug)}</span></div><ul class="todos sug">{"".join(map(_todo, sug))}</ul>'
    if done:
        out += f'<details class="todo-done" id="todo-done"><summary>Done <span class="count">{len(done)}</span></summary><ul class="todos">{"".join(map(_todo, done))}</ul></details>'
    return len(mine), out


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

    n_todo, todo_html = _todos(state.get("todos") or [])

    updated = state.get("updated_at") or time.time()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(project_name)} · Atrium Board</title>
<style id="board-css">{CSS}</style></head>
<body><div class="wrap">
<header class="top" data-region="top">
  <div>
    <div class="eyebrow">Atrium Board</div>
    <h1>{_e(project_name)}</h1>
    {f'<p class="headline">{_e(ov.get("headline"))}</p>' if ov.get('headline') else ''}
  </div>
  <div class="meta"><b>{len(agents)}</b> agents · <b>{active}</b> active<br>updated {_ts(updated)}<br><span title="{_e(project_path)}">{_e(project_path)}</span></div>
</header>
<section class="overview" data-region="overview">{ov.get('html') or ''}</section>
<div data-region="agents"><h2>Agents <span class="count">{len(agents)}</span></h2>
<section class="agents">{tiles}</section></div>
<div class="cols">
  <div class="c-results" data-region="results"><h2>Results <span class="count">{len(cards)}</span></h2><section class="results">{card_html}</section></div>
  <div class="c-todo">
    <h2 data-region="todo-head">TODO <span class="count">{n_todo}</span></h2>
    <div class="todo-add"><input id="todo-input" type="text" placeholder="Add a TODO…" maxlength="300" enterkeyhint="done" autocomplete="off" aria-label="New TODO"><button data-act="add" aria-label="Add" title="Add">+</button></div>
    <div data-region="todos">{todo_html}</div>
  </div>
  <div class="c-timeline" data-region="timeline"><h2>Timeline</h2><ul class="timeline">{tl_html}</ul></div>
</div>
</div>
<script id="board-js">{JS}</script>
</body></html>
"""
