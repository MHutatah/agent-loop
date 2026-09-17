#!/usr/bin/env bash
# Poke the overseer so the loop keeps moving without anyone typing.
#
# WHY THIS EXISTS. The systemd timers run agents and open pull requests, but
# queueing the next issue and merging a finished one are the overseer's
# decisions, and the overseer is an interactive Claude Code session: it does
# nothing at all unless a message arrives. So the loop would implement whatever
# was already labelled, then sit still, looking healthy and making no progress.
# That is the failure this removes.
#
# It send-keys into the EXISTING session rather than starting a new one, so a
# spell of related work stays in one conversation. But an existing session is
# not free, and the next block is why.
set -u
W=agents:overseer
OVERSEER_DIR=$HOME/agent-loop/overseer
MAX_TRANSCRIPT=$((1024 * 1024))          # 1 MiB, about four times the floor

tmux has-session -t agents 2>/dev/null || exit 0
tmux list-windows -t agents -F '#{window_name}' | grep -qx overseer || exit 0

# Never interrupt a session that is mid-thought or waiting on an answer. A
# send-keys into a running turn lands as stray text in whatever it is doing.
PANE=$(tmux capture-pane -p -t "$W" 2>/dev/null | tail -5)
case "$PANE" in
  *"esc to interrupt"*)      exit 0 ;;
  *"Do you want to proceed"*) exit 0 ;;
  *"Enter to confirm"*)       exit 0 ;;
esac

# CLEAR THE SESSION ONCE IT IS BIG, because an hourly poke into one endless
# conversation re-charges that whole conversation every time.
#
# This script used to argue the opposite: "a fresh session each hour would
# re-read the repository and re-litigate the same calls". Measured on
# 2026-09-17, that session had reached 1587 messages and 2.5 MB, and almost all
# of it was the same one-line report, "unchanged, same six PRs held", repeated
# hourly. Cache reads are around 97% of this account's token spend, so history
# that says nothing is the most expensive thing here.
#
# The premise was also wrong. Nothing the overseer must remember lives in the
# conversation: its judgement is in overseer/CLAUDE.md and the state is in
# GitHub's own labels, comments and issue bodies, which it re-reads with `gh`
# on every check anyway. Verified rather than assumed: after a /clear the
# overseer was asked, without reading any files, for its escalation rule and
# the production boundary, and it answered with the 800-line backstop, the
# touch-first rule, the --ignore-cr-at-eol measurement and "shipping is yours".
# The brief is re-read; the history was not carrying it.
#
# So: clear past a ceiling rather than on every poke. Under the ceiling a
# working spell keeps its context, which is worth something when it has just
# merged one pull request and is about to look at two more.
PROJ=$HOME/.claude/projects/$(printf '%s' "$OVERSEER_DIR" | sed 's#/#-#g')
NEWEST=$(ls -t "$PROJ"/*.jsonl 2>/dev/null | head -1)
if [ -n "${NEWEST:-}" ] && [ "$(stat -c %s "$NEWEST" 2>/dev/null || echo 0)" -gt "$MAX_TRANSCRIPT" ]; then
  tmux send-keys -t "$W" "/clear"
  sleep 2
  tmux send-keys -t "$W" Enter
  sleep 6
fi

MSG='Routine check. Queue any issue whose "Depends on" are all closed, a few at a time and in dependency order. Merge any pull request that qualifies under your brief, and escalate the ones that do not rather than merging them. If nothing needs doing, say so in one line and stop.'

tmux send-keys -t "$W" "$MSG"
sleep 2
tmux send-keys -t "$W" Enter
