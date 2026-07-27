#!/usr/bin/env bash
# The overseer window: an interactive Claude session that knows about the loop.
# Respawns if it exits, so the window is always there when you attach from a phone.
cd "$HOME/agent-loop/overseer" || exit 1
while true; do
  clear
  printf '\033[1;36m overseer \033[0m — ask about the agents. Ctrl-b w to switch windows.\n\n'
  claude --model sonnet
  printf '\n\033[2msession ended — restarting in 3s (Ctrl-c to stop)\033[0m\n'
  sleep 3
done
