"""Recognising a usage limit, with the exact strings seen in production.

Closes the recognition half of #19.

WHY THIS FILE EXISTS AS ITS OWN SUITE. The mechanism was never broken:
`_collect_finished` has always released a limited run without a reason, so no
attempt is burned and no help is called for. Only `looks_limited` was wrong, by
one word — the pattern said `usage limit` and the CLI says "You've hit your
SESSION limit". Between 02:00 and 04:00 on 2026-09-20 that labelled ten
ipa-community issues `agent:needs-human`, and the loop then sat idle for
seventeen hours with a full queue and nothing actually wrong with any of them.

A bug in recognition is the worst kind here, because every part that reads
correctly still does nothing. So these are the observed messages, pinned as
literals: if a CLI rewords its limit notice, this suite fails rather than the
loop quietly parking a day's work.
"""
from agentloop.runner import looks_limited

# Copied from tmux output and issue comments, not paraphrased.
OBSERVED = [
    "You've hit your session limit · resets 4am (UTC)",
    "You've hit your session limit · resets 2:10am (UTC)",
    "You've hit your usage limit · resets 4am (UTC)",
    "Claude usage limit reached",
    "5-hour limit reached ∙ resets 3pm",
    "429 Too Many Requests",
    "rate limit exceeded, please try again later",
    "You have exceeded your quota",
]

# Things that must NOT be read as a limit, or a real failure gets retried
# forever instead of reaching a human.
FAILURES = [
    "TypeError: Cannot read properties of undefined (reading 'map')",
    "npm ERR! Test failed. See above for more details.",
    "fatal: couldn't find remote ref agent/79-story-list-the-licensing-stages",
    "AssertionError: expected 1 but got 2",
    "SyntaxError: Unexpected end of JSON input",
    "Error: database is not open",
    "",
]


def test_every_observed_limit_message_is_recognised():
    missed = [m for m in OBSERVED if not looks_limited(m)]
    assert missed == [], f"a real limit notice went unrecognised: {missed}"


def test_the_exact_message_that_parked_ten_issues():
    """Named on its own because it is the regression, not an example of one."""
    assert looks_limited("You've hit your session limit · resets 4am (UTC)")


def test_an_ordinary_failure_is_not_mistaken_for_a_limit():
    """The other direction matters just as much. A crash read as a limit is
    retried on every tick forever and never reaches anybody."""
    wrong = [m for m in FAILURES if looks_limited(m)]
    assert wrong == [], f"these are failures, not limits: {wrong}"


def test_the_word_limit_alone_is_not_enough():
    """`limit` appears in ordinary output — a rate-limiter's own tests, a
    docstring, a CLI's help text — so matching it bare would swallow real
    failures. The pattern requires a limit PHRASE, not the word."""
    assert not looks_limited("the limit is 26 weeks, and a larger one is refused")
    assert not looks_limited("def test_limit(): assert cap == 3")
