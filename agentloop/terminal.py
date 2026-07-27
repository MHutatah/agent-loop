"""An in-app terminal view with its own key bar.

A phone keyboard has no Esc, no Tab, no Ctrl and no arrows — the keys a TUI is
driven by. The usual answer is "install a terminal app that provides them", which
is precisely the extra-app friction this console exists to remove.

So rather than embedding a terminal emulator, this drives tmux directly:
`capture-pane` to read, `send-keys` to write. Text comes from the phone's own
keyboard (which works fine); everything a phone keyboard lacks is a button. The
result needs no xterm.js, no websocket, and no second app.

Reading and writing are separate HTTP calls, so this is a slow-motion terminal,
not an emulator — right for "ask the overseer a question and read the answer",
wrong for vim. ttyd remains available for the latter.
"""
from __future__ import annotations

import html
import subprocess

SESSION = "agents"

# Keys a touchscreen cannot produce. Each entry is (label, tmux key, css class).
# tmux key names are passed as a single argv element — never through a shell.
KEYPAD: list[tuple[str, str, str]] = [
    ("Esc", "Escape", ""),
    ("Tab", "Tab", ""),
    ("↑", "Up", ""),
    ("↓", "Down", ""),
    ("←", "Left", ""),
    ("→", "Right", ""),
    ("Ctrl-C", "C-c", "danger"),
    ("Ctrl-D", "C-d", "danger"),
    ("Ctrl-R", "C-r", ""),
    ("Enter", "Enter", "go"),
    ("⇧Tab", "BTab", ""),
    ("Home", "Home", ""),
    ("End", "End", ""),
    ("PgUp", "PageUp", ""),
    ("PgDn", "PageDown", ""),
    ("Space", "Space", ""),
]
VALID_KEYS = {k for _, k, _ in KEYPAD}


def windows() -> list[str]:
    rc, out = _tmux(["list-windows", "-t", SESSION, "-F", "#{window_name}"])
    return out.split() if rc == 0 else []


def _tmux(args: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        p = subprocess.run(["tmux", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=timeout)
        return p.returncode, (p.stdout or "").rstrip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def capture(window: str, lines: int = 200) -> str:
    """Current pane contents. Trailing blank lines are dropped so the newest
    output sits at the bottom of the view rather than above a wall of padding."""
    rc, out = _tmux(["capture-pane", "-t", f"{SESSION}:{window}", "-p",
                     "-S", f"-{lines}"])
    if rc != 0:
        return f"(cannot read window {window!r} — is the session running?)"
    return "\n".join(out.splitlines()).rstrip() or "(empty)"


def send_text(window: str, text: str, enter: bool = True) -> tuple[str, bool]:
    """Type into the window. `-l` sends the text literally, so a message
    containing tmux key names or a semicolon is typed, not interpreted."""
    if not text.strip():
        return "nothing to send", False
    rc, _ = _tmux(["send-keys", "-t", f"{SESSION}:{window}", "-l", text])
    if rc != 0:
        return "could not reach that window", False
    if enter:
        _tmux(["send-keys", "-t", f"{SESSION}:{window}", "Enter"])
    return f"sent to {window}", True


def send_key(window: str, key: str) -> tuple[str, bool]:
    """Send one named key. Rejects anything not on the pad, so this endpoint
    can never be coaxed into sending arbitrary input."""
    if key not in VALID_KEYS:
        return f"unknown key: {key}", False
    rc, _ = _tmux(["send-keys", "-t", f"{SESSION}:{window}", key])
    return (f"sent {key}", True) if rc == 0 else ("could not reach that window", False)


def render(window: str, css: str, flash: str = "", ok: bool = True) -> str:
    """The terminal page. Output is a <pre> so scrolling, selection and
    find-in-page are the browser's own; input is the phone's keyboard plus a pad
    for the keys it doesn't have."""
    pane = capture(window)
    tabs = "".join(
        f'<a class="tab{" on" if w == window else ""}" href="/term?w={w}">{html.escape(w)}</a>'
        for w in windows())
    keys = "".join(
        f'<form method=post action="/key/{window}/{k}">'
        f'<button class="k {cls}">{html.escape(label)}</button></form>'
        for label, k, cls in KEYPAD)

    return f"""<!doctype html><html lang=en><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=theme-color content="#0b1221">
<link rel=manifest href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{html.escape(window)}</title><style>{css}
.tabs{{display:flex;gap:6px;overflow-x:auto;padding:2px 0 0;-webkit-overflow-scrolling:touch}}
.tab{{flex:none;padding:8px 12px;border-radius:99px;background:#16203a;color:var(--dim);
 font-size:13px;text-decoration:none;min-height:36px;display:flex;align-items:center}}
.tab.on{{background:#1f3a2a;color:#86efac}}
/* The terminal is sized for a phone: small enough that a TUI's columns survive,
   large enough to read. Users can still pinch-zoom. */
pre.term{{font:11px/1.35 ui-monospace,Menlo,Consolas,monospace;background:var(--sunk);
 color:#c7d3e5;padding:10px;border-radius:var(--rs);white-space:pre;overflow-x:auto;
 -webkit-overflow-scrolling:touch;margin:0;min-height:44vh;max-height:52vh;
 overflow-y:auto}}
.composer{{position:fixed;left:0;right:0;bottom:0;z-index:6;background:#0b1221f5;
 backdrop-filter:blur(8px);border-top:1px solid var(--line);
 padding:8px 10px calc(8px + env(safe-area-inset-bottom))}}
.pad{{display:flex;gap:6px;overflow-x:auto;padding-bottom:8px;
 -webkit-overflow-scrolling:touch}}
.pad form{{flex:none;margin:0}}
button.k{{min-width:56px;min-height:44px;padding:0 12px;font-size:14px;
 border-radius:10px;background:var(--btn)}}
button.k.danger{{background:#3a1717;color:#ffd9d9}}
button.k.go{{background:#14331f;color:#86efac}}
.say{{display:flex;gap:6px}}
.say input{{flex:1;min-height:48px;border-radius:var(--r);border:1px solid var(--line);
 background:#0f1830;color:var(--ink);padding:0 14px;font:15px/1 inherit}}
.say button{{flex:none;min-width:80px}}
body{{padding-bottom:calc(150px + env(safe-area-inset-bottom))}}
</style></head><body>
<header>
  <h1>{html.escape(window)}<span class="st"> · terminal</span></h1>
  <div class="tabs">{tabs}</div>
</header>
<main>
  {f'<div class="flash{"" if ok else " err"}">{html.escape(flash)}</div>' if flash else ''}
  <a class="btn" href="/" style="margin-bottom:10px">&lsaquo; Console</a>
  <pre class="term" id=term>{html.escape(pane)}</pre>
</main>
<div class="composer">
  <div class="pad">{keys}</div>
  <form class="say" method=post action="/say/{window}">
    <input name=t autocomplete=off autocapitalize=sentences
           placeholder="Message {html.escape(window)}…" aria-label="Message">
    <button class="go">Send</button>
  </form>
</div>
<script>
 const term = document.getElementById('term');
 term.scrollTop = term.scrollHeight;             // newest output, like a terminal
 // Poll just the pane text. A full reload would lose the input you were typing.
 setInterval(async () => {{
   if (document.hidden || document.activeElement.tagName === 'INPUT') return;
   try {{
     const r = await fetch('/api/pane?w={window}', {{cache:'no-store'}});
     const d = await r.json();
     const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
     if (term.textContent !== d.pane) {{
       term.textContent = d.pane;
       if (atBottom) term.scrollTop = term.scrollHeight;   // don't yank you back
     }}
   }} catch (e) {{}}
 }}, 3000);
</script></body></html>"""
