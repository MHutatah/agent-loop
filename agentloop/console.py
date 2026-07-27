"""A phone-shaped web console for the loop.

Why this exists: a terminal is the wrong surface for a phone. xterm.js has known,
unfixed touch limitations, and tmux's mouse mode fights the browser for scroll
events — so reading a long agent log on a phone ranges from awkward to impossible.

Everything here is ordinary HTML instead. Scrolling, pinch-zoom, text selection,
browser find-in-page and back-button all work because they are the browser's own,
not an emulation of them. Control is buttons, so nothing needs a Ctrl key.

Two rules the earlier version broke, both found in review:

* **Never reload the document to refresh.** A meta-refresh collapsed every open
  log, threw away scroll position and find-in-page state every 20 seconds, and
  re-shipped ~375 KB to update a status line. Only the header polls now, as a
  small JSON fragment patched in place.
* **Never render unbounded content.** Logs are fetched on demand from /log/<n>,
  cards are capped, and a merged issue's log is discarded — otherwise page weight
  and per-render disk reads grow with the project's lifetime, unattended.

Deliberately stdlib-only and single-file: it runs next to the loop on a small ARM
box, and a console that needs its own dependency tree is a console that breaks.
"""
from __future__ import annotations

import html
import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote_plus, urlparse

from agentloop import gh, terminal, tmux
from agentloop.budget import Budget
from agentloop.config import (
    LABEL_NEEDS_HUMAN,
    LABEL_PR,
    LABEL_READY,
    Config,
    touches_guarded_path,
)
from agentloop.settings import BOUNDS, Settings
from agentloop.usage import codex_usage

WORKSPACE = Path.home() / "agent-loop-work"
MAX_CARDS = 8              # newest N; the rest are history, not status
LOG_TAIL = 60_000          # what /log/<n> serves — read by seek, not full file
PREVIEW_TAIL = 2_000       # the "last output" line on a card

# Actions the buttons map to. Nothing here takes free-form input: the console can
# only invoke this fixed set, so a stray tap can't become an arbitrary command.
ACTIONS: dict[str, list[str]] = {
    "pause": ["sudo", "systemctl", "stop", "agentloop-issues.timer",
              "agentloop-prs.timer"],
    "resume": ["sudo", "systemctl", "start", "agentloop-issues.timer",
               "agentloop-prs.timer"],
    "tick-issues": ["python3", "-m", "agentloop", "issues"],
    "tick-prs": ["python3", "-m", "agentloop", "prs"],
    "restart-overseer": ["bash", "-lc",
                         "tmux kill-window -t agents:overseer 2>/dev/null; "
                         "tmux new-window -d -t agents: -n overseer "
                         "$HOME/agent-loop/deploy/overseer/start.sh"],
}

# Handled in-process rather than by shelling out.
INTERNAL = {"budget-reset", "judge+", "judge-", "agents+", "agents-",
            "tries+", "tries-"}
# Take an issue number: /do/kill/17, /do/requeue/23
PARAMETERISED = {"kill", "requeue"}

# ── PWA ──────────────────────────────────────────────────────────────────────
MANIFEST = {
    "name": "agent-loop", "short_name": "agents", "start_url": "/", "scope": "/",
    "display": "standalone", "orientation": "portrait",
    "background_color": "#0b1221", "theme_color": "#0b1221",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
               "purpose": "any maskable"}],
}

ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<rect width="192" height="192" rx="42" fill="#0b1221"/>
<circle cx="96" cy="76" r="30" fill="none" stroke="#22c55e" stroke-width="10"/>
<circle cx="96" cy="76" r="9" fill="#22c55e"/>
<path d="M52 150c0-24 20-40 44-40s44 16 44 40" fill="none" stroke="#6ea8fe"
 stroke-width="10" stroke-linecap="round"/></svg>"""

SW = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));"""

CSS = """
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
 --bg:#0b1221; --card:#111a2e; --sunk:#070d19; --line:#1e2a44; --btn:#1b2740;
 --ink:#e8edf5; --dim:#9fb0c8; --mute:#7d8799;
 --ok:#22c55e; --warn:#eab308; --bad:#ef4444; --info:#6ea8fe;
 --r:12px; --rs:8px;
}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 padding:env(safe-area-inset-top) 0 calc(72px + env(safe-area-inset-bottom))}
header{position:sticky;top:0;z-index:5;background:#0b1221ee;backdrop-filter:blur(8px);
 padding:14px 16px 11px;border-bottom:1px solid var(--line)}
h1{margin:0;font:700 22px/1.2 inherit;letter-spacing:-.02em}
h1 .st{font-size:14px;font-weight:600;color:var(--mute);letter-spacing:0}
.sub{color:var(--mute);font-size:12.5px;margin-top:4px}
main{padding:14px 16px 24px;max-width:820px;margin:0 auto}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
button,a.btn{flex:1 1 auto;min-width:118px;min-height:48px;border:0;
 border-radius:var(--r);background:var(--btn);color:var(--ink);
 font:600 14px/1 inherit;padding:14px 12px;text-align:center;text-decoration:none;
 display:flex;align-items:center;justify-content:center;gap:6px;cursor:pointer}
button:active,a.btn:active{background:#26355a}
button.warn{background:#3a2a1b}button.go{background:#14331f}
button.danger{background:#3a1717;color:#ffd9d9}
button[disabled]{opacity:.5}
:focus-visible{outline:2px solid var(--info);outline-offset:2px;border-radius:var(--rs)}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
 padding:13px 14px;margin:0 0 11px}
.card h2{margin:0 0 3px;font:600 15px/1.3 inherit;display:flex;align-items:center;gap:8px}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.live{background:var(--ok);box-shadow:0 0 0 3px #22c55e28}
.done{background:var(--info)}.bad{background:var(--bad)}
.meta{color:var(--mute);font-size:12.5px}
.last{margin-top:8px;font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;
 color:var(--dim);background:var(--sunk);border-radius:var(--rs);padding:9px 10px;
 white-space:pre-wrap;word-break:break-word;max-height:6.5em;overflow:hidden}
a.more{display:inline-block;margin-top:9px;color:var(--info);font-size:13.5px;
 text-decoration:none;min-height:44px;line-height:44px}
pre.log{font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;background:var(--sunk);
 color:#c7d3e5;padding:12px;border-radius:var(--rs);white-space:pre-wrap;
 word-break:break-word;margin:0}
.empty{color:var(--mute);text-align:center;padding:26px 0}
.gauge{margin:0 0 12px}
.gauge .top{display:flex;justify-content:space-between;align-items:baseline;
 font-size:13px;margin-bottom:6px;gap:8px}
.gauge .top span{color:var(--mute);font-size:12px;text-align:right}
.bar{height:9px;border-radius:6px;background:#16203a;overflow:hidden}
.bar i{display:block;height:100%;border-radius:6px;background:var(--ok)}
.bar i.mid{background:var(--warn)}.bar i.hot{background:var(--bad)}
.stepper{display:flex;align-items:center;gap:8px;margin-top:10px}
.stepper form{flex:none}
.stepper button{min-width:56px;min-height:44px;padding:0;font-size:20px;line-height:1}
.stepper .val{flex:1;text-align:center;font-size:13px;color:var(--dim)}
.stepper .rng{color:var(--mute)}
.flash{background:#14331f;border:1px solid #22c55e44;border-radius:var(--r);
 padding:11px 13px;margin:0 0 14px;font-size:13.5px;white-space:pre-wrap;
 word-break:break-word}
.flash.err{background:#3a1414;border-color:#ef444455}
.alert{background:#3a2a1b;border:1px solid #eab30855;border-radius:var(--r);
 padding:12px 13px;margin:0 0 14px}
.alert a{color:#ffd8a8;display:block;padding:6px 0;font-size:14px}
.pill{font-size:11.5px;padding:2px 8px;border-radius:99px;background:#16203a;
 color:var(--dim);font-weight:600}
.pill.ok{background:#14331f;color:#86efac}.pill.bad{background:#3a1717;color:#fca5a5}
.pill.wait{background:#3a2a1b;color:#fcd34d}
.bottom{position:fixed;left:0;right:0;bottom:0;z-index:6;display:flex;gap:8px;
 padding:8px 12px calc(8px + env(safe-area-inset-bottom));
 background:#0b1221f2;backdrop-filter:blur(8px);border-top:1px solid var(--line)}
.bottom form{flex:1}.bottom button{width:100%;min-width:0}
"""


def _sh(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[str, bool]:
    """Run one action. Returns (output, ok) — the caller must be able to tell a
    failure from a success, because they are styled differently."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=cwd, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        out = ((p.stdout or "") + (p.stderr or "")).strip() or "(no output)"
        return out, p.returncode == 0
    except Exception as exc:                              # noqa: BLE001
        return f"failed: {exc}", False


def _timers_running() -> bool:
    """Whether the loop is scheduled. Any failure reads as 'not running' rather
    than raising — a missing systemctl must not turn the whole page into a 500."""
    try:
        p = subprocess.run(["systemctl", "is-active", "agentloop-issues.timer"],
                           capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
        return (p.stdout or "").strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


# ── a small TTL cache ────────────────────────────────────────────────────────
# render() makes several blocking `gh` round-trips. Without this, a reload right
# after a poll pays for all of them again.
_CACHE: dict[str, tuple[float, object]] = {}


def cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


# ── state gathering ──────────────────────────────────────────────────────────
class State:
    """Everything the page needs, plus whether we could actually see it.

    `online` matters: previously a GitHub outage rendered as "0 open PR", so being
    blind looked exactly like having nothing to do.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # A fresh clone has no repos configured yet. That is the very first thing
        # a new user sees, so it must render an instruction rather than crash.
        self.repo = cfg.repos[0] if cfg.repos else None
        self.online = True
        if self.repo is None:
            self.ready = self.prs = self.stuck = []
            return
        self.ready: list[dict] = []
        self.prs: list[dict] = []
        self.stuck: list[dict] = []
        try:
            self.ready = cached("ready", 15, lambda: gh.ready_issues(
                self.repo.slug, LABEL_READY, "agent:working", "agent:stop"))
            self.prs = cached("prs", 15,
                              lambda: gh.open_prs(self.repo.slug, LABEL_PR))
            self.stuck = cached("stuck", 15, lambda: gh.issues_with_label(
                self.repo.slug, LABEL_NEEDS_HUMAN))
        except Exception:                                 # noqa: BLE001
            self.online = False

    @property
    def headline(self) -> str:
        """The answer to 'is anything wrong', not an inventory."""
        if self.repo is None:
            return "No repository configured"
        if not self.online:
            return "Can't reach GitHub"
        if self.stuck:
            return f"{len(self.stuck)} need you"
        if not _timers_running():
            return "Paused"
        live = len(tmux.live_windows())
        if live:
            return f"{live} agent{'s' if live > 1 else ''} working"
        if self.prs:
            return f"{len(self.prs)} PR{'s' if len(self.prs) > 1 else ''} in flight"
        return "All clear"


def pr_status(repo: str, pr: dict) -> tuple[str, str]:
    """(pill class, human sentence) for why this PR has not merged.

    gate.decide() already computes precisely this every tick and then throws it
    away; the console had shown a bare PR number, so the only way to learn why
    nothing merged was to leave for GitHub.
    """
    num = pr["number"]
    try:
        checks = cached(f"chk{num}", 20,
                        lambda: gh.pr_checks_state(repo, num))
        comments = cached(f"cmt{num}", 20,
                          lambda: gh.pr_review_comments(repo, num))
        files = cached(f"fil{num}", 60, lambda: gh.pr_files(repo, num))
    except Exception:                                     # noqa: BLE001
        return "wait", "status unavailable"

    if (pr.get("mergeable") or "").upper() == "CONFLICTING":
        return "bad", "merge conflict — needs a rebase"
    guarded = touches_guarded_path(files, Config.load())
    if guarded:
        return "wait", f"held: touches {guarded[0]}"
    if checks == "fail":
        return "bad", "CI failing"
    if checks == "pending":
        return "wait", "CI running"
    if checks == "none":
        return "wait", "no CI configured — won't merge blind"
    if checks == "unknown":
        return "wait", "can't read CI"
    verdict = gh.pr_judged_verdict(comments, pr.get("headRefOid") or "")
    if verdict is None:
        return "wait", "waiting for review"
    if verdict is False:
        return "bad", "reviewer asked for changes"
    return "ok", "approved — merging shortly"


# ── fragments ────────────────────────────────────────────────────────────────
def _bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    cls = "hot" if pct >= 85 else "mid" if pct >= 60 else ""
    return f'<div class="bar"><i class="{cls}" style="width:{pct:.0f}%"></i></div>'


def _stepper(key: str, label: str, value: int, minus: str, plus: str) -> str:
    lo, hi = BOUNDS[key]
    low = label.lower()
    return f"""<div class="stepper">
      <form method=post action="/do/{minus}"><button
        aria-label="decrease {low}">&minus;</button></form>
      <div class="val">{label}: <b>{value}</b>
        <span class="rng">({lo}&ndash;{hi})</span></div>
      <form method=post action="/do/{plus}"><button
        aria-label="increase {low}">+</button></form>
    </div>"""


def _alerts(st: State) -> str:
    if st.repo is None:
        return ('<div class="alert"><b>No repository configured.</b>'
                '<div class="meta">Add one to <code>agentloop.toml</code>:<br>'
                '<code>[[repo]]<br>slug = "you/your-repo"<br>'
                'auto_merge = false</code><br>then restart the console.</div></div>')
    if not st.online:
        return ('<div class="alert"><b>Can\'t reach GitHub.</b>'
                '<div class="meta">Counts below may be stale or missing — this is '
                'not the same as having nothing to do.</div></div>')
    if not st.stuck:
        return ""
    items = "".join(
        f'<a href="{html.escape(i.get("url", "#"))}">#{i["number"]} '
        f'{html.escape((i.get("title") or "")[:60])}</a>' for i in st.stuck[:6])
    return (f'<div class="alert"><b>{len(st.stuck)} need you</b>'
            f'<div class="meta">Stopped and waiting on a human.</div>{items}</div>')


def _issues_link(st: State) -> str:
    if not st.repo:
        return ""
    return f'<a class="btn" href="https://github.com/{st.repo.slug}/issues">Issues</a>'


def _pr_cards(st: State) -> str:
    if not st.prs:
        return ""
    out = []
    for pr in st.prs[:6]:
        cls, why = pr_status(st.repo.slug, pr)
        out.append(f"""<div class="card">
          <h2>PR #{pr['number']} <span class="pill {cls}">{html.escape(why)}</span></h2>
          <div class="meta">{html.escape((pr.get('title') or '')[:70])}</div>
          <a class="more" href="https://github.com/{st.repo.slug}/pull/{pr['number']}"
             >Open on GitHub &rsaquo;</a>
        </div>""")
    return "".join(out)


def _usage_panel(cfg: Config, budget: Budget, sett: Settings) -> str:
    cu = codex_usage()
    if cu.ok:
        w = cu.primary
        codex = f"""<div class="gauge">
          <div class="top"><b>Codex &middot; {w.label}</b>
            <span>{w.used_percent:.0f}% used &middot; resets in {w.resets_in}</span></div>
          {_bar(w.used_percent)}</div>"""
    else:
        codex = f'<div class="meta">Codex usage unavailable — {html.escape(cu.error)}</div>'

    used, cap = budget.used(), cfg.max_judge_calls
    pct = (used / cap * 100) if cap else 100
    resets = f" &middot; resets in {budget.resets_in_min()}m" if used else ""
    claude = f"""<div class="gauge">
      <div class="top"><b>Claude &middot; judge budget</b>
        <span>{used}/{cap} in {cfg.judge_window_hours:.0f}h{resets}</span></div>
      {_bar(pct)}</div>"""

    return f"""<details class="card"><summary>Usage &amp; limits</summary>
      <div style="margin-top:10px">{codex}{claude}
      <div class="meta">Codex is the account's real server-side limit. The Claude
        figure is the cap this loop imposes on itself.</div>
      {_stepper("max_judge_calls", "Judge calls", sett.max_judge_calls, "judge-", "judge+")}
      {_stepper("max_concurrent_agents", "Concurrent agents",
                sett.max_concurrent_agents, "agents-", "agents+")}
      {_stepper("max_attempts_per_issue", "Attempts per issue",
                sett.max_attempts_per_issue, "tries-", "tries+")}
      <div class="row" style="margin:12px 0 0">
        <form method=post action="/do/budget-reset" style="flex:1">
          <button style="width:100%">Reset judge budget</button></form>
        <form method=post action="/do/restart-overseer" style="flex:1">
          <button class="warn" style="width:100%"
            onsubmit="return confirm('Restart the overseer?')"
            >Restart overseer</button></form>
      </div></div></details>"""


def _agent_cards(logs: Path, st: State) -> str:
    """Newest N only. Rendering one card per issue ever run made both page weight
    and per-render disk reads grow without bound."""
    live = set(tmux.live_windows())
    try:
        known = {int(f.stem.split("-")[1]) for f in logs.glob("issue-*.log")
                 if f.stem.split("-")[1].isdigit()}
    except OSError:
        known = set()
    seen = sorted(known | live, reverse=True)
    hidden = max(0, len(seen) - MAX_CARDS)
    seen = seen[:MAX_CARDS]
    if not seen:
        return '<div class="empty">No agents have run yet.</div>'

    out = []
    for n in seen:
        running = n in live
        tail = tmux.output(n, logs, tail=PREVIEW_TAIL)
        lines = [s for s in tail.splitlines() if s.strip()]
        preview = "\n".join(lines)[-260:] or "…"
        failed = (not running) and any(
            k in tail.lower() for k in ("traceback", "error:", "exited with code"))
        cls = "live" if running else ("bad" if failed else "done")
        state = "working" if running else ("finished with errors" if failed
                                           else "finished")
        kill = (f'<form method=post action="/do/kill/{n}" style="flex:1">'
                f'<button class="danger" style="width:100%">Stop this agent</button>'
                f'</form>') if running else ""
        out.append(f"""<div class="card">
          <h2><span class="dot {cls}" aria-hidden="true"></span>issue #{n}</h2>
          <div class="meta">{state} &middot; {tmux.log_size(n, logs) // 1024} KB log</div>
          <div class="last">{html.escape(preview)}</div>
          <a class="more" href="/log/{n}">Full log &rsaquo;</a>
          <div class="row" style="margin:8px 0 0">{kill}</div>
        </div>""")
    if hidden:
        out.append(f'<div class="meta" style="text-align:center">'
                   f'{hidden} older run{"s" if hidden > 1 else ""} not shown</div>')
    return "".join(out)


def header_fragment(st: State, budget: Budget) -> dict:
    """What the 20s poll swaps in. A few hundred bytes, not the whole document."""
    return {
        "headline": st.headline,
        "running": _timers_running(),
        "sub": (f"{len(st.ready)} ready &middot; {len(st.prs)} open "
                f"PR{'s' if len(st.prs) != 1 else ''} &middot; {budget.status()}"),
    }


def render(cfg: Config, flash: str = "", ok: bool = True) -> str:
    st = State(cfg)
    budget = Budget(WORKSPACE / "judge-budget.json", cfg.max_judge_calls,
                    cfg.judge_window_hours)
    sett = Settings.load()
    frag = header_fragment(st, budget)
    running = frag["running"]

    return f"""<!doctype html><html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#0b1221">
<link rel=manifest href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.svg">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="agents">
<title>agent-loop</title><style>{CSS}</style></head><body>
<header>
  <h1 id=hl aria-live="polite">{html.escape(frag['headline'])}
    <span class="st">{'· running' if running else '· paused'}</span></h1>
  <div class="sub" id=sub>{frag['sub']}</div>
</header>
<main>
  {f'<div class="flash{"" if ok else " err"}">{html.escape(flash)}</div>' if flash else ''}
  {_alerts(st)}
  <div class="row">
    <form method=post action="/do/tick-issues"><button>Check issues</button></form>
    <form method=post action="/do/tick-prs"><button>Check PRs</button></form>
    {_issues_link(st)}
    <a class="btn" href="/term">Terminal</a>
  </div>
  {_pr_cards(st)}
  {_agent_cards(WORKSPACE / 'logs', st)}
  {_usage_panel(cfg, budget, sett)}
</main>
<div class="bottom">
  <form method=post action="/do/{'pause' if running else 'resume'}">
    <button class="{'warn' if running else 'go'}">
      {'Pause loop' if running else 'Resume loop'}</button></form>
</div>
<script>
 if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
 // Poll a small fragment instead of reloading the document: a full reload
 // collapsed every open log and threw away scroll position every 20 seconds.
 setInterval(async () => {{
   if (document.hidden) return;                 // don't burn data in the background
   try {{
     const r = await fetch('/api/header', {{cache: 'no-store'}});
     const d = await r.json();
     document.getElementById('hl').innerHTML =
       d.headline + ' <span class="st">' + (d.running ? '· running' : '· paused') + '</span>';
     document.getElementById('sub').innerHTML = d.sub;
   }} catch (e) {{ /* offline: keep showing the last known values */ }}
 }}, 20000);
 // Disable a button once tapped: actions can take up to two minutes, and an
 // apparently-dead tap invites a second one that races the first.
 document.addEventListener('submit', e => {{
   const b = e.target.querySelector('button');
   if (b) {{ b.disabled = true; b.textContent = 'Working…'; }}
 }});
</script></body></html>"""


def render_log(issue: int) -> str:
    text = tmux.output(issue, WORKSPACE / "logs", tail=LOG_TAIL)
    return f"""<!doctype html><html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#0b1221">
<title>issue #{issue} log</title><style>{CSS}</style></head><body>
<header><h1>issue #{issue}<span class="st"> · log</span></h1>
<div class="sub">last {len(text) // 1024} KB &middot; newest at the bottom</div></header>
<main><a class="btn" href="/" style="margin-bottom:12px">&lsaquo; Back</a>
<pre class="log">{html.escape(text) or '(empty)'}</pre></main>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    cfg = Config.load()

    def _send(self, body: str, code: int = 200,
              ctype: str = "text/html; charset=utf-8", cache: str = "no-store") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:                             # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q0 = parse_qs(parsed.query)
        if path == "/manifest.webmanifest":
            return self._send(json.dumps(MANIFEST), ctype="application/manifest+json",
                              cache="public, max-age=86400")
        if path == "/icon.svg":
            return self._send(ICON, ctype="image/svg+xml",
                              cache="public, max-age=86400")
        if path == "/sw.js":
            return self._send(SW, ctype="text/javascript",
                              cache="public, max-age=86400")
        if path == "/api/header":
            cfg = Config.load()
            st = State(cfg)
            b = Budget(WORKSPACE / "judge-budget.json", cfg.max_judge_calls,
                       cfg.judge_window_hours)
            return self._send(json.dumps(header_fragment(st, b)),
                              ctype="application/json")
        if path == "/term":
            w = (q0.get("w") or ["overseer"])[0]
            if w not in terminal.windows():
                w = "overseer"
            return self._send(terminal.render(
                w, CSS, flash=(q0.get("m") or [""])[0],
                ok=(q0.get("ok") or ["1"])[0] != "0"))
        if path == "/api/pane":
            w = (q0.get("w") or ["overseer"])[0]
            if w not in terminal.windows():
                return self._send(json.dumps({"pane": "(no such window)"}),
                                  ctype="application/json")
            return self._send(json.dumps({"pane": terminal.capture(w)}),
                              ctype="application/json")
        if path.startswith("/log/"):
            try:
                return self._send(render_log(int(path.rsplit("/", 1)[-1])))
            except ValueError:
                return self._send(render(self.cfg, "no such log", ok=False), 404)

        q = parse_qs(urlparse(self.path).query)
        # parse_qs has ALREADY percent-decoded the value. Decoding again turns a
        # literal "%41" in command output into "A" — so the fix for one escaping
        # bug quietly introduced another. One decode, not two.
        flash = (q.get("m") or [""])[0]
        ok = (q.get("ok") or ["1"])[0] != "0"
        self._send(render(self.cfg, flash, ok))

    def do_POST(self) -> None:                            # noqa: N802
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts and parts[0] in ("say", "key"):
            return self._terminal_post(parts)
        name = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""
        ok = True
        if name in PARAMETERISED:
            out, ok = self._parameterised(name, arg)
        elif name in INTERNAL:
            out = self._internal(name)
        elif name in ACTIONS:
            out, ok = _sh(ACTIONS[name], cwd=str(Path.home() / "agent-loop"))
        else:
            return self._send(render(self.cfg, f"unknown action: {name}", ok=False), 400)
        _CACHE.clear()                       # an action just changed the world
        self.send_response(303)
        self.send_header("Location",
                         f"/?ok={'1' if ok else '0'}&m={quote(out[-400:], safe='')}")
        self.end_headers()

    def _terminal_post(self, parts: list[str]) -> None:
        """Typing and key presses go back to /term, not to the console."""
        window = parts[1] if len(parts) > 1 else "overseer"
        if window not in terminal.windows():
            return self._redirect("/term", "no such window", False)
        if parts[0] == "key":
            out, ok = terminal.send_key(window, parts[2] if len(parts) > 2 else "")
        else:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            text = unquote_plus((parse_qs(body).get("t") or [""])[0])
            out, ok = terminal.send_text(window, text)
        self._redirect(f"/term?w={window}", out, ok)

    def _redirect(self, base: str, msg: str, ok: bool) -> None:
        sep = "&" if "?" in base else "?"
        self.send_response(303)
        self.send_header("Location",
                         f"{base}{sep}ok={'1' if ok else '0'}&m={quote(msg[-400:], safe='')}")
        self.end_headers()

    def _parameterised(self, name: str, arg: str) -> tuple[str, bool]:
        try:
            n = int(arg)
        except ValueError:
            return f"{name}: not an issue number", False
        if not self.cfg.repos:
            return "no repository configured", False
        repo = self.cfg.repos[0].slug
        if name == "kill":
            tmux.kill(n)
            return f"stopped the agent on #{n}", True
        # requeue: hand a stalled issue back to the loop
        try:
            gh.remove_label(repo, n, LABEL_NEEDS_HUMAN)
            gh.add_label(repo, n, LABEL_READY)
            return f"#{n} queued again", True
        except Exception as exc:                          # noqa: BLE001
            return f"could not requeue #{n}: {exc}", False

    def _internal(self, name: str) -> str:
        cfg = Config.load()
        if name == "budget-reset":
            Budget(WORKSPACE / "judge-budget.json", cfg.max_judge_calls,
                   cfg.judge_window_hours).reset()
            return "judge budget cleared"
        sett = Settings.load()
        key, delta = {
            "judge+": ("max_judge_calls", 1), "judge-": ("max_judge_calls", -1),
            "agents+": ("max_concurrent_agents", 1),
            "agents-": ("max_concurrent_agents", -1),
            "tries+": ("max_attempts_per_issue", 1),
            "tries-": ("max_attempts_per_issue", -1),
        }[name]
        return sett.adjust(key, delta)

    def log_message(self, *a) -> None:                    # quieter journal
        pass


def serve(host: str = "127.0.0.1", port: int = 7682) -> None:
    """Loopback by default. `tailscale serve` fronts this with HTTPS and is the
    only route in; defaulting to a routable address would expose it to the whole
    tailnet the moment someone ran it without arguments."""
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 7682   # argv is always str
    serve(host, port)
