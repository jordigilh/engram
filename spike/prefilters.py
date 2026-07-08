"""Two candidate recall-oriented prefilters for the correction-detection
shadow trial (see docs/FINDINGS.md 2026-07-08 entry for why: Variant A's
embedding-similarity gate underperformed regex/Haiku-alone, so the next
candidate for cutting Haiku call volume is a cheap, deterministic,
inspectable filter instead of a similarity threshold).

CRITICAL: neither of these gates anything for real. They are evaluated in
shadow mode alongside an unconditional Haiku call (the reference/control,
already proven at 0.97 F1 in the spike) so their real-world recall can be
measured against Haiku's own judgment on live traffic -- not against
ground_truth.py, which would be circular (that set was itself discovered via
keyword scanning).

Both filters answer "should this message go to Haiku?" (True = send it).
They are deliberately asymmetric in risk:
  - loose_regex_prefilter: POSITIVE signal net. Cast wide on purpose --
    happy to have low precision, must not silently have low recall.
  - trivial_message_exclusion_filter: NEGATIVE signal net. Only excludes
    messages with near-zero plausibility of being a correction. Much more
    conservative than the regex net; intended as a safer, smaller win.
"""
from __future__ import annotations

import re

# Deliberately broader than nightly-learn.py's CORRECTION_PATTERNS (which is
# tuned for precision as a direct detector). This list is tuned for recall as
# a pre-Haiku candidate gate -- false positives here just cost an extra cheap
# Haiku call; false negatives here silently hide a real correction from
# Haiku entirely, which is the failure mode that matters.
_LOOSE_PATTERNS = [
    re.compile(r"\bwrong\b", re.I),
    re.compile(r"\bincorrect(ly)?\b", re.I),
    re.compile(r"\bnot\s+(correct|right|following|aligned|what)\b", re.I),
    re.compile(r"\bshould\s+(have|be|not)\b", re.I),
    re.compile(r"\bdon'?t\s+(do|use)\b", re.I),
    re.compile(r"\bdo\s+not\s+(use|do)\b", re.I),
    re.compile(r"\bwe\s+don'?t\b", re.I),
    re.compile(r"\bnever\s+use\b", re.I),
    re.compile(r"\bstill\s+not\b", re.I),
    re.compile(r"\bkeep\s+\w+ing\b", re.I),
    re.compile(r"\bmistak(e|ing)\b", re.I),
    re.compile(r"\bmisunderstood\b", re.I),
    re.compile(r"\bmisread\b", re.I),
    re.compile(r"\brevert\b", re.I),
    re.compile(r"\bundo\b", re.I),
    re.compile(r"\bwhy\s+did\s+you\b", re.I),
    re.compile(r"\bwhy\s+(is|does|didn'?t)\b", re.I),
    re.compile(r"\bthat'?s\s+not\b", re.I),
    re.compile(r"\bnot\s+what\s+i\b", re.I),
    re.compile(r"\bconfus", re.I),
    re.compile(r"\bforgot\b", re.I),
    re.compile(r"\byou\s+missed\b", re.I),
    re.compile(r"\bwithout\s+permission\b", re.I),
    re.compile(r"\bunauthorized\b", re.I),
    re.compile(r"\bagain,?\s+you\b", re.I),
    re.compile(r"\bnevermind\b|\bnever\s?mind\b", re.I),  # also lets "nevermind" through for observation
    re.compile(r"\bstop\b", re.I),
]


def loose_regex_prefilter(text: str) -> bool:
    """True if text matches any broad correction-adjacent cue."""
    return any(p.search(text) for p in _LOOSE_PATTERNS)


_TRIVIAL_ACK_RE = re.compile(
    r"^(ok|okay|yes|no|sure|thanks|thank you|thx|got it|sounds good|"
    r"perfect|great|nevermind|never mind|continue|proceed|go ahead|"
    r"carry on|do it|please continue|lgtm|ack)[.!]?$",
    re.I,
)
_BARE_URL_RE = re.compile(r"^https?://\S+$", re.I)


def trivial_message_exclusion_filter(text: str) -> bool:
    """True (send to Haiku) unless the message is an obvious non-candidate:
    a bare short acknowledgment, a bare URL, or under 4 characters.
    Deliberately conservative -- only excludes near-zero-plausibility cases.
    """
    stripped = text.strip()
    if len(stripped) < 4:
        return False
    if _TRIVIAL_ACK_RE.match(stripped):
        return False
    if _BARE_URL_RE.match(stripped):
        return False
    return True
