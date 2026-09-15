# Where this should go, and what the evidence actually says

Written September 2026, after the multi-repo and collector fixes. Every claim
below was checked against a primary source or reproduced in the code, and where
the evidence does not support a decision this file says so rather than dressing
a preference as a finding.

## 0. The one-line summary

The design is better than the runtime. `gate.py` is the valuable part and should
outlive everything around it. The scheduler, the tmux state machine, the `.rc`
protocol and the label-as-lock are a re-implementation of GitHub Actions, and
the single fact that made that re-implementation necessary is no longer true.

## 1. The premise that no longer holds

The whole system runs on a box because `codex exec` and `claude -p` authenticate
against consumer subscriptions, and the judge's docstring is explicit that this
protects a Pro plan's quota. The assumption was that CI cannot do that.

**It can.** `claude-code-action` accepts `claude_code_oauth_token`, an OAuth
token generated locally with `claude setup-token`, "available on Pro, Max, Team,
and Enterprise plans", and Anthropic's own cost guidance states: "If you
authenticate with an OAuth token, runs use your Claude subscription instead of
API billing."

Two consequences:

- The reason for the bespoke runtime was the subscription, and the subscription
  works in Actions. What remains on the box is a *preference*, not a constraint.
- The action also runs in **automation mode**: give it a `prompt` input and it
  runs on any GitHub event with no `@claude` mention. `issues: labeled` is the
  trigger this project polls for every ten minutes.

One real caveat, and it interacts badly with our gate: **GitHub does not trigger
workflows on commits made with the default `GITHUB_TOKEN`.** `gate.py` refuses
to merge when `checks == "none"` ("no CI checks configured — refusing to merge
blind"), which is correct, so a naive port produces PRs that can never merge.
The fix is documented: let the action authenticate as the Claude GitHub App, or
pass a custom app token.

## 2. Actions concurrency is the lock, but not the queue

This is the finding that changed a recommendation I had already made out loud,
so it is worth stating precisely.

`concurrency` looks like a work queue and is not one. From GitHub's own
documentation: by default "any existing `pending` job or workflow in the same
concurrency group will be canceled and the new queued job or workflow will take
its place." `cancel-in-progress: false` protects only the *running* job. The
`queue` property controls the rest: `single` (the default) allows at most one
pending run, and `max` allows "up to 100 jobs or workflow runs" pending, with
anything beyond that cancelled. `queue: max` with `cancel-in-progress: true` is
a validation error.

So:

- **One concurrency group per issue**, `issue-${{ github.event.issue.number }}`,
  is exactly the `agent:working` lock, and it is atomic in a way a label read
  followed by a label write can never be. Eviction cannot bite, because no two
  runs for the same issue should both proceed anyway.
- **Never use one shared group for capacity.** With `queue: single` a second
  labelled issue silently evicts the first one's pending run, which is the same
  lost-work bug the reaper was just written to fix, reintroduced at a different
  layer. Cap capacity with the size of the runner pool, or `queue: max`.

## 3. What the empirical literature supports, and what it does not

Ogenrwot and Businge, *How AI Coding Agents Modify Code: A Large-Scale Study of
GitHub Pull Requests* (arXiv:2601.17581v3, April 2026), compares 24,014 merged
agentic PRs against 5,081 merged human PRs. Usable findings:

- Agentic PRs use substantially fewer commits (Cliff's δ = 0.5429, a large
  effect) and touch fewer files (δ = 0.4487), i.e. "smaller and more localized
  changes than Human PRs".
- Agents differ from each other: Claude Code and Codex produce broader edits,
  GitHub Copilot "highly localized edits".
- Description-to-diff semantic alignment is slightly *higher* for agentic PRs.

**What it does not show**, and must not be cited for: review time, rework,
rejection or abandonment rates, test coverage, defects, or reverts. It studies
*merged* PRs only, so it is silent on the question we actually care about, which
is how often an agentic PR is wrong in a way review has to catch. Anyone
claiming this literature proves agents are safe to auto-merge is over-reading
it, and so would I be.

The one design conclusion it does support: agents do well on small, localized,
well-described units of work. That argues for keeping issues narrow, which is
what one-story-per-issue with explicit acceptance criteria already does.

## 4. The judge was the weakest part, and the fix was free

`judge.py` promised to decide whether "an acceptance criterion is unmet, or met
only superficially" while being handed the diff as text, with no repository and
no tools, deliberately, to keep it to one cheap call. That is not a cheap
version of the task; it is a different, weaker task. It cannot see the function
a changed line calls, or whether a new test asserts anything that would fail if
the change were reverted.

Fixed here by giving the judge the worktree and `Read,Grep,Glob`, read-only, so
it cannot edit the branch it is judging, and no test execution, because CI is
already the authority on that and `gate.py` refuses to merge without it.

For the Actions path, Anthropic ships the reviewer for this job: the
`code-review` plugin, invoked as a skill through `claude-code-action`, which
reads the repository and posts inline comments on the PR. Prefer it over a
hand-rolled prompt.

## 5. Off-the-shelf alternatives, honestly assessed

| Option | Why it is attractive | Why it does not simply replace this |
|---|---|---|
| **Copilot coding agent** | Assign an issue, get a PR. GA since March 2026, runs on GitHub Actions, zero ops | A second paid subscription; least control over the prompt and the gate |
| **`claude-code-action`** | Official, subscription auth, automation mode, runs skills and plugins | Hosted runners cannot hold a `codex` login, so a Codex implementer needs a self-hosted runner anyway |
| **OpenHands** | MIT, self-hostable, has a GitHub issue resolver | Another runtime to operate, which is the problem we are trying to delete |
| **Build on Actions ourselves** | Events, ephemeral workspaces, secrets, logs, retries, per-issue locks, all for free | Needs the `GITHUB_TOKEN`/CI caveat in §1 handled or nothing ever merges |

## 6. Recommended order

1. **Done: the correctness fixes.** Per-repo isolation, the reaper, idempotent
   collection, containment before the push, per-repo capacity and rotation.
   These are worth having in any architecture that keeps worktrees.
2. **Self-hosted runner on the box, Actions as the control plane.** Keep the
   subscription CLIs where they are authenticated; delete the timers, the `.rc`
   protocol, the label-as-lock and most of the console. Labelling an issue from
   the GitHub mobile app is the same control surface the console was built to
   provide.
3. **Port `gate.py` as a required check** and let branch protection plus
   GitHub's own auto-merge enforce the policy, instead of Python deciding and
   then calling `gh pr merge`. The policy is the asset; the enforcement should
   not be ours.
4. **Only then** consider adding a second implementer or more projects. More
   parallelism over a runtime you do not trust multiplies the failures rather
   than the output.

## Sources

- Claude Code GitHub Actions: <https://code.claude.com/docs/en/github-actions>
- Control workflow concurrency:
  <https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency>
- Ogenrwot & Businge, arXiv:2601.17581v3: <https://arxiv.org/html/2601.17581v3>
- Copilot coding agent GA:
  <https://github.com/orgs/community/discussions/159068>
