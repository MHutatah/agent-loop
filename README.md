# agent-loop

An autonomous **GitHub issue → PR** loop that runs on a server you own, driven by
**your own Claude and Codex subscriptions** rather than metered API keys.

Label an issue. An agent implements it in an isolated worktree, opens a PR, and a
*second* model reviews that PR against the issue's acceptance criteria. If CI is
green and the review passes, it merges itself.

Your laptop stays off. From your phone the whole workflow is: **label → read → merge.**

```
issue labelled agent:ready
        │
        ▼
   git worktree ──►  Codex implements  ──►  tests  ──►  PR opened
                                                          │
                                                          ▼
                                              Claude judges the diff
                                              against the acceptance criteria
                                                          │
                              ┌───────────────────────────┴──────────────┐
                        passes + CI green                          blocks
                              │                                          │
                        auto-merge                            Codex fixes it, loops
```

**One model builds, a different one judges.** They don't share a context, so the
reviewer isn't marking its own homework. The split is also budget-driven: Codex
has the roomier subscription quota, and judging is a single cheap call that reads
the diff as text — no repo, no tools — so your Claude plan stays free for your own
work.

## The phone console

Everything is reachable from a browser: a PWA you can install to your home screen.

- **What's happening** — leads with *"2 need you"* / *"All clear"* / *"Paused"*, not a wall of counts
- **Why a PR is stuck** — *"held: touches .github/workflows/ci.yml"*, *"merge conflict"*, *"CI failing"*
- **Live agent logs**, loaded on tap, with native scrolling and find-in-page
- **A real terminal** with an on-screen key bar (Esc, Tab, Ctrl-C, arrows) — because a phone keyboard has none of them, and needing a second app for that is the friction this replaces
- **Controls** — pause, force a tick, stop one agent, requeue a stalled issue, tune budgets

It is deliberately plain HTML: a terminal emulator is the wrong surface for a
phone, so scrolling, zoom, selection and find are the browser's own rather than an
emulation of them.

## Install

Needs a Linux host with `git`, `tmux`, `python3.11+`, `gh`, and the two agent CLIs
logged in to your subscriptions.

```bash
git clone https://github.com/MHutatah/agent-loop && cd agent-loop
cp agentloop.example.toml agentloop.toml    # name your repo
./install.sh
```

`install.sh` is idempotent — re-run it to redeploy. It installs the systemd units,
creates the workspace, and grants bubblewrap the namespace permission Codex's
sandbox needs on Ubuntu 24.04+ (to that binary alone, not host-wide).

**It does not start the loop.** Look it over first:

```bash
python3 -m agentloop doctor              # CLIs, auth, config
python3 -m agentloop status              # what it can see
python3 -m agentloop issues --dry-run    # walk the whole loop, change nothing
```

Then, when you're ready:

```bash
sudo systemctl enable --now agentloop-issues.timer agentloop-prs.timer
```

### Authentication

Both CLIs authenticate against consumer subscriptions, not API keys:

- **Claude** — `claude auth login` on the host, or `claude setup-token` on a machine
  with a browser and put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`
- **Codex** — `codex login` on a machine with a browser, then copy `~/.codex/auth.json`
  to the host (simpler than reverse-tunnelling the OAuth callback)

### Exposing the console

It binds to loopback. **Put it behind something that authenticates you** — a
Tailscale/WireGuard network, or a reverse proxy with auth. The terminal view is
writable, so its network boundary is the only thing protecting a shell:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:7682
```

## Control surface

Labels, so you can drive it from the GitHub mobile app:

| Label | Meaning |
|---|---|
| `agent:ready` | **You add this.** An agent picks the issue up. |
| `agent:working` | In flight (prevents double-starts) |
| `agent:pr` | A PR this system owns |
| `agent:needs-human` | Gave up, hit the attempt cap, or touched a guarded path |
| `agent:stop` | **Kill switch** — agents will not touch it |

## Guardrails

Auto-merge is **opt-in per repo** and requires *every* one of these — see
[`agentloop/gate.py`](agentloop/gate.py), tested in [`tests/test_gate.py`](tests/test_gate.py):

- CI is green. **No CI configured is a refusal**, not a pass — nothing verified the change.
- The judge returned a genuine pass. An errored or unparseable verdict is **not** a pass.
- No human has commented on the PR.
- No merge conflict.
- **No guarded path touched** — `.github/workflows/`, licences, `deploy/`, `.env`, and
  the loop's own config. An agent must not be able to weaken the guardrails, or
  disable the CI that gates it, unattended.

Plus: a cap on concurrent agents, a cap on attempts per issue, a rolling budget on
judge calls so the loop can't exhaust the quota you use yourself, and rate limits
treated as *retry later* rather than failure.

Most of the test suite exists to assert what must **not** merge.

## Configuration

| Where | What | Changing it needs |
|---|---|---|
| `agentloop.toml` | which repos, whether each may auto-merge | an edit |
| console UI | budget, concurrency, attempts | a tap |
| `agentloop/config.py` | guarded paths, models, the gate | a deploy |

That split is deliberate. A web button can move a number within declared bounds;
it cannot widen what agents may touch.

## Development

```bash
pip install -e ".[dev]"
pytest -q && ruff check agentloop tests
```

No live network in the test suite — the agent CLIs and `gh` are never called.

## Licence

MIT.
