#!/usr/bin/env bash
# Build the tmux session the phone attaches to: overseer first (what you usually
# want to talk to), dashboard second, agent windows appear as they spawn.
set -u
D="$HOME/agent-loop/deploy"

if ! tmux has-session -t agents 2>/dev/null; then
  tmux -f "$D/tmux.conf" new-session -d -s agents -n overseer "$D/overseer/start.sh"
fi

if ! tmux list-windows -t agents -F '#{window_name}' | grep -qx dashboard; then
  tmux new-window -d -t agents: -n dashboard "$D/dashboard.sh"
fi

# Re-source on every start so config changes land without killing the session.
tmux source-file "$D/tmux.conf" 2>/dev/null || true
tmux select-window -t agents:overseer
