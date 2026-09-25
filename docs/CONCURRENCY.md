# How many builders to run

Standing default: **one**. Raised only when asked for, and only with a reason
from one of the trees below.

This file exists because "more agents" feels like more throughput and the
measurements say it is not. It is written to be read at a status check, in
thirty seconds, without re-deriving anything.

## The one fact that changes every answer

**The five-hour window is rolling and starts at your first message, and what
you do not spend in it does not carry over.**

That single property produces both halves of the advice, which look
contradictory until you see why:

- Spending fast *early* in a window is bad. It buys nothing and costs the
  remaining hours, which you then sit out.
- Spending fast *late* in a window is good. That budget is about to expire.

So the question is never "is parallelism efficient". It is "how much of this
window is left, and how much of it am I going to lose".

## Measured constants

From this deployment, not from a blog. Weighted tokens mean
`input + 1.25x cache_write + 0.1x cache_read + 5x output`, which is the shape
of what a window counts.

| Quantity | Measured |
|---|---|
| One window's usable budget | ~27M weighted |
| Burn rate, 4 concurrent builders | ~0.68M weighted/min |
| Burn rate, 1 builder | ~0.17M weighted/min |
| Window drained at 4 builders | ~40 min, then 4h20m idle |
| Window drained at 1 builder | ~160 min |
| **Agent-minutes per window, either way** | **~160** |
| Loop call cost | 15.5K weighted |
| Interactive session call cost, 500K context | 68.5K weighted |
| Loop spend, heaviest day | 124.7M weighted, 8,047 calls |

**Read the bold row twice.** Four builders for forty minutes and one builder
for one hundred and sixty minutes are the same number of agent-minutes. The
window is the budget; concurrency only decides how fast you reach the bottom
of it. Concurrency does not buy throughput here. It buys or loses two other
things: idle time, and rework from contention.

## Tree 1: should concurrency go up right now?

Walk it in order. The first stop wins.

```
1. Is the weekly cap the binding limit, not the 5-hour window?
   YES -> stay at 1. A weekly cap has no expiry to race, so spending
           fast only moves the wall closer. STOP.
   NO  -> continue.

2. Is the ready work contended? Two or more issues touching the same
   file, the same module, or the same epic?
   YES -> stay at 1, whatever the budget says. Contention converts
           parallelism into merge conflicts and rework, and rework is
           spent twice. Measured here: #139, #143 and #144 all edit
           app/path/page.tsx; lib/db.ts was contended by six open PRs.
           Use _spread_by_area first, and if it cannot find distinct
           areas, there are none. STOP.
   NO  -> continue.

3. How much of the current window is left?
   >3h remaining  -> stay at 1. There is time to spend it all at a
                      pace that keeps working. STOP.
   1-3h remaining -> stay at 1 unless tree 2 says the budget will
                      expire unspent. Go to tree 2.
   <1h remaining  -> go to tree 2. This is the case where raising it
                      is usually right.

4. Reached here from tree 2 with "budget will expire unspent"?
   -> raise to 2-4 for the remainder of the window, on genuinely
      independent issues only. Drop back to 1 at the reset.
```

## Tree 2: will the budget expire unspent?

```
Estimate remaining budget:
  spent_so_far  = weighted tokens since the window's first message
  remaining     = 27M - spent_so_far        (see measured constants)
  minutes_left  = 300 - minutes since first message

  one_builder_will_use = 0.17M * minutes_left

  IF remaining > one_builder_will_use * 1.3
     -> budget WILL expire unspent. Raising concurrency converts
        expiring budget into work. Raise to:
           ceil(remaining / (0.17M * minutes_left))
        capped at 4, and capped by the number of genuinely
        independent issues available.

  IF remaining <= one_builder_will_use
     -> one builder already fills the window. Raising it only
        shortens the window and buys idle time. Stay at 1.
```

Worked example, the case that prompted this file: at reset with 27M and 300
minutes, one builder will use 0.17M x 300 = 51M, which is more than 27M. So one
builder already over-fills a fresh window and concurrency is never right at the
start of one. At 40 minutes left with 20M unspent, one builder will use 6.8M
and 13M would expire, so 3 builders is right for that last stretch.

## Tree 3: is this work parallelisable at all?

Anthropic's own guidance, applied to this repo. Multi-agent costs 3 to 10 times
the tokens for equivalent work, so the work has to actually divide.

```
Does the work decompose into independent parts with clean interfaces?
  NO  -> 1 agent. "Sequential phases of the same feature share too
         much context" and belong in one agent.
  YES -> does each part need the same files?
         YES -> 1 agent. Shared-file work is not independent, it is
                serialised work with extra conflicts.
         NO  -> does each part need its own large reading pass?
                YES -> parallel pays: separate contexts are the point,
                       and this is the research-shaped case.
                NO  -> 1 agent. Coordination overhead exceeds the gain.
```

Coding sits on the "no" branch far more often than research does. That is
Anthropic's finding and it matches this repo: the epics overlap on `lib/db.ts`,
`app/path/page.tsx` and the architecture documents.

## Tree 4: what to do when the limit has already been hit

This is now automatic, and the trees above do not apply until it clears.

```
A usage limit was reported
  -> cooldown.note_limit() parses the reset time out of the notice
     and holds EVERY repo until then. Account-wide, because one
     subscription funds them all.
  -> collection and reaping keep running: they cost no tokens and
     free state a limit makes more valuable.
  -> nothing is spawned, judged or fixed until the reset passes.
```

Before this existed the loop retried on the next tick, ten minutes away, into
a limit with hours left on it: 238 builder spawns in three days, 178 of them
killed inside two minutes, the same eight issues respawned 25 to 28 times each.
Those doomed spawns each paid for a full prompt prefix before reading the
refusal, which is why `cache_creation` was 22 to 28% of everything the loop
spent. Honouring the server's own reset time is the documented best practice,
the same rule as obeying `Retry-After` rather than guessing.

## What to say at a status check

Report these four, in this order, and give a recommendation rather than a menu:

1. Minutes left in the current window, and roughly what fraction of the budget
   is unspent.
2. Whether the ready work is contended (run `_spread_by_area`, or just look at
   whether the open PRs touch the same files).
3. The concurrency that follows from trees 1 and 2.
4. Whether a cooldown is currently in force, and until when.

If the answer is "stay at 1", say so in one line and move on. The default is
correct most of the time and does not need defending every time.

## Sources

Measurements are from this deployment's own transcripts and journal, September
2026. External guidance:

- [Anthropic, How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system):
  agents use ~4x the tokens of chat and multi-agent ~15x; token usage alone
  explains 80% of performance variance; "most coding tasks involve fewer truly
  parallelizable tasks than research".
- [Anthropic, When to use multi-agent systems (and when not to)](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them):
  start with a single agent; multi-agent uses "3-10x more tokens ... for
  equivalent tasks"; decompose by context boundary, not by problem type;
  "planning, implementation, and testing of the same feature share too much
  context".
- [Claude Code usage limits explained](https://bestagent.dev/claude-code-usage-limits/)
  and [Claude usage limits 2026](https://www.ai-toolbox.co/claude-management-and-productivity/claude-usage-limits-2026):
  the five-hour window is rolling from the first message; paid plans add a
  weekly cap; chats, Desktop and Claude Code draw from one pool.
- [AWS Well-Architected, Control and limit retry calls](https://docs.aws.amazon.com/en_us/wellarchitected/2022-03-31/framework/rel_mitigate_interaction_failure_limit_retries.html)
  and [Exponential backoff and jitter](https://betterstack.com/community/guides/monitoring/exponential-backoff/):
  honour the server's own retry guidance, cap attempts, and never retry at full
  speed into a limit.
