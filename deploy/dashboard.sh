#!/usr/bin/env bash
# The window you land on when you open the terminal on your phone.
#
# Deliberately a narrow, high-contrast summary rather than a wall of text: on a
# phone screen you want "what is happening and is anything stuck", and you switch
# to an agent's own window only when the answer is interesting.
set -u
cd "$(dirname "$0")/.." || exit 1
LOGS="${AGENTLOOP_WORKSPACE:-$HOME/agent-loop-work}/logs"

while true; do
  clear
  printf '\033[1;36m agent-loop \033[0m  %s\n' "$(date '+%a %H:%M:%S')"
  printf '\033[2m%s\033[0m\n' "──────────────────────────────────────"

  # live agents, newest activity first
  printf '\033[1mAGENTS\033[0m\n'
  mapfile -t wins < <(tmux list-windows -t agents -F '#{window_name} #{pane_dead}' 2>/dev/null | grep '^issue-' || true)
  if [ "${#wins[@]}" -eq 0 ]; then
    printf '  \033[2midle — no agents running\033[0m\n'
  else
    for w in "${wins[@]}"; do
      name="${w%% *}"; dead="${w##* }"
      if [ "$dead" = "0" ]; then
        printf '  \033[1;32m●\033[0m %-12s \033[2mworking\033[0m\n' "$name"
      else
        printf '  \033[1;33m○\033[0m %-12s \033[2mfinished\033[0m\n' "$name"
      fi
    done
  fi

  # last line each agent printed — the "is it stuck" signal
  printf '\n\033[1mLAST OUTPUT\033[0m\n'
  if compgen -G "$LOGS/issue-*.log" >/dev/null 2>&1; then
    for f in "$LOGS"/issue-*.log; do
      n=$(basename "$f" .log)
      line=$(tail -n 1 "$f" 2>/dev/null | tr -d '\r' | cut -c1-46)
      printf '  \033[36m%-12s\033[0m %s\n' "$n" "${line:-…}"
    done
  else
    printf '  \033[2m(none yet)\033[0m\n'
  fi

  printf '\n\033[2mCtrl-b w = pick a window · Ctrl-b n/p = next/prev\033[0m\n'
  sleep 5
done
