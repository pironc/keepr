"""Normalized-pattern detection for greetings and farewells.

Catches simple conversational openers/closers *before* embedding, retrieval,
or LLM inference — the user gets an instant canned response instead of
paying several seconds of local-model latency for an answer that has zero
retrieval dependency and one obvious answer.

Only the *entire* user input is considered — a greeting embedded in a real
question ("hi, what's the capital?") passes through unchanged, same as
every other question.
"""

from __future__ import annotations

import re

_GREETING_RESPONSE = "Hello! How can I help you?"
_FAREWELL_RESPONSE = "Goodbye! Feel free to come back anytime."

# Patterns that match the *entire* input after normalization (lowercase,
# stripped of leading/trailing punctuation and whitespace, internal
# whitespace collapsed to single spaces).
#
# Looser than exact-string matching — handles "Hi!", "  hey there  ",
# "how's it going?" etc. — but tight enough that "hi, what's the capital
# of France?" is never absorbed (the comma + question content diverges from
# every pattern here).

_GREETING_PATTERN = re.compile(
    r"""(?x)
    (?:hi|hey|heya|hello|howdy|hiya(?:h)?|yo|sup|what's\ up|what\ is\ up
    |good\ (?:morning|afternoon|evening|day)
    |g(?:')?day|greetings|salutations
    |how\ (?:are\ (?:you|u|ya)|r\ (?:you|u|ya)|is\ it\ going|(?:are\ )?things(?:\ going)?)
    |how(?:'s|\ is)\ it\ going
    |how\ (?:do\ you\ do|ya\ doin'?|you\ doin'?)
    |what's\ (?:good|new|happening|goin'\ on)|what\ is\ (?:good|new)
    |nice\ to\ (?:meet|see)\ you|pleased\ to\ meet\ you
    |good\ to\ see\ you)
    [.!?…,]*$
    """
)

_FAREWELL_PATTERN = re.compile(
    r"""(?x)
    (?:bye|good\ ?bye|see\ (?:ya|you|u)(?:\ later|(?:\ around)?)?
    |(?:catch\ you\ )?later|cya|ttfn|ttyl
    |thanks|thank\ you|thank\ you\ very\ much|(?:many\ )?thanks|thx|cheers
    |take\ care|have\ a\ good\ (?:one|day|night|evening|weekend)
    |good\ night|good\ ?night
    |peace(?:\ out)?
    |farewell|until\ (?:next\ time|we\ meet\ again)
    |(?:i(?:')?m|i\ am)\ (?:off|heading\ (?:off|out)|outta\ here|out\ of\ here)
    |gotta\ (?:go|run|bounce|jet|dip|split)
    |talk\ (?:to\ you\ )?later|talk\ soon)
    [.!?…,]*$
    """
)


def detect_greeting_or_farewell(question: str) -> str | None:
    """Return a canned response if `question` is entirely a greeting or
    farewell, or `None` if normal processing should proceed.

    The check is narrow by design: a real question that happens to open with
    "hi" or "thanks" will not match, because the *entire* input must reduce
    to one of the known patterns.
    """
    # Normalize: lowercase, collapse whitespace, strip leading/trailing
    # punctuation and whitespace — "  Hi!  " -> "hi".
    normalized = question.strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)  # collapse internal whitespace
    normalized = normalized.strip("!.,;:…? ")  # strip framing punctuation

    if not normalized:
        return None

    if _GREETING_PATTERN.match(normalized):
        return _GREETING_RESPONSE
    if _FAREWELL_PATTERN.match(normalized):
        return _FAREWELL_RESPONSE

    return None
