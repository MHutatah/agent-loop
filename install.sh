#!/usr/bin/env bash
# One-command setup for a fresh Linux host.
#
# Everything here is idempotent — re-running it after a change is the intended
# way to redeploy. It installs nothing behind your back and prints what it did.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
HOME_DIR="$(eval echo "~$USER_NAME")"
WORKSPACE="${AGENTLOOP_WORKSPACE:-$HOME_DIR/agent-loop-work}"
BIND="${AGENTLOOP_BIND:-127.0.0.1}"

# The web terminal is writable, so what it binds to is what protects your shell.
# A tailnet interface is the safe default when one exists; loopback otherwise.
if [ -z "${AGENTLOOP_TTYD_IFACE:-}" ]; then
  if ip -o link show tailscale0 >/dev/null 2>&1; then IFACE=tailscale0; else IFACE=lo; fi
else
  IFACE="$AGENTLOOP_TTYD_IFACE"
fi

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m x\033[0m %s\n' "$*" >&2; exit 1; }

# ── prerequisites ────────────────────────────────────────────────────────────
say "Checking prerequisites"
missing=()
for t in git tmux python3 gh; do command -v "$t" >/dev/null || missing+=("$t"); done
[ ${#missing[@]} -eq 0 ] || die "install these first: ${missing[*]}"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
  || die "python 3.11+ required (tomllib)"

for t in codex claude; do
  command -v "$t" >/dev/null \
    || warn "$t not found — install it and log in, or the loop cannot $([ "$t" = codex ] && echo implement || echo judge)"
done

gh auth status >/dev/null 2>&1 || die "run: gh auth login"
# gh must clone over HTTPS: with git_protocol=ssh and no key registered, cloning
# fails in a way that looks like a permissions problem.
gh auth setup-git >/dev/null 2>&1 || true

# ── config ───────────────────────────────────────────────────────────────────
if [ ! -f "$HERE/agentloop.toml" ]; then
  cp "$HERE/agentloop.example.toml" "$HERE/agentloop.toml"
  warn "created agentloop.toml — edit it to name your repo, then re-run"
fi

mkdir -p "$WORKSPACE/logs"
[ -f "$HERE/.env" ] || { touch "$HERE/.env"; chmod 600 "$HERE/.env"; }

# ── the sandbox Codex needs on Ubuntu 24.04+ ─────────────────────────────────
# Unprivileged user namespaces are restricted by default, which is exactly what
# bubblewrap needs. Grant it to that binary alone rather than disabling the
# protection host-wide — the host may be running other things.
if [ -f /etc/apparmor.d/ ] || command -v apparmor_parser >/dev/null 2>&1; then
  if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = "1" ] \
     && [ ! -f /etc/apparmor.d/bwrap ]; then
    say "Allowing bubblewrap to create sandboxes (AppArmor profile)"
    sudo cp "$HERE/deploy/apparmor-bwrap" /etc/apparmor.d/bwrap
    sudo apparmor_parser -r /etc/apparmor.d/bwrap || warn "apparmor reload failed"
  fi
fi

# ── systemd units ────────────────────────────────────────────────────────────
say "Installing systemd units for $USER_NAME"
tmp="$(mktemp -d)"
for f in "$HERE"/deploy/*.service "$HERE"/deploy/*.timer; do
  sed -e "s|__USER__|$USER_NAME|g" \
      -e "s|__HOME__|$HOME_DIR|g" \
      -e "s|__WORKSPACE__|$WORKSPACE|g" \
      -e "s|__BIND__|$BIND|g" \
      -e "s|__IFACE__|$IFACE|g" \
      "$f" > "$tmp/$(basename "$f")"
done

# A placeholder that slips through installs a unit that cannot start — and
# systemd reports the service "active" while it restart-loops, so the failure is
# quiet. Fail here instead. (Shipped once: __IFACE__ took ttyd down.)
if grep -l '__[A-Z]*__' "$tmp"/* 2>/dev/null | grep -q .; then
  grep -H '__[A-Z]*__' "$tmp"/* >&2
  die "unsubstituted placeholder above — add it to the sed list"
fi
sudo cp "$tmp"/* /etc/systemd/system/
rm -rf "$tmp"
sudo systemctl daemon-reload

# ── go ───────────────────────────────────────────────────────────────────────
say "Starting the console and terminal view"
sudo systemctl enable --now agentloop-console agentloop-view >/dev/null

cat <<EOF

  Console   http://$BIND:7682     (put it behind a TLS proxy before using it)
  Terminal  in-app at /term, or ttyd on :7681

  The loop itself is NOT started yet — that is deliberate. Check it over first:

    python3 -m agentloop doctor    # CLIs, auth, config
    python3 -m agentloop status    # what it can see
    python3 -m agentloop issues --dry-run   # walk the loop, change nothing

  When you are ready to let it run:

    sudo systemctl enable --now agentloop-issues.timer agentloop-prs.timer

  Then label an issue 'agent:ready' and watch.

EOF
