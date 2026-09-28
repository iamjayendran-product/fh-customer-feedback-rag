"""PII redaction and author hashing.

Applied to review text before it is stored, tagged by an LLM, or embedded.
We keep rating/date/source/market — never display names — and hash the
author handle so records can still be deduplicated/traced back to "same
person, multiple reviews" without keeping an identifying string.
"""
from __future__ import annotations

import hashlib
import re

# A fixed local salt is fine here: this is a research dataset, not an auth
# system, and the goal is stable de-dup hashing, not resistance to a
# determined attacker with a rainbow table.
_SALT = "fh-feedback-v1"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?(?:\(0\)\s?)?(?:\d[\s.-]?){8,12}\d"
)
_ORDER_ID_RE = re.compile(
    r"\b(?:order|ref(?:erence)?)\s*#?\s*(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{4,}\b"
    r"|#(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{3,}\b",
    re.IGNORECASE,
)
# Note: requires a digit in the trailing token, or this matches plain English
# words after "order"/"ref" ("order tracking", "refunding") and eats them —
# found live in batch_0001 tagging (see feedback-tagging run notes,
# 2026-09-24) where "refunding took ages" became "[ORDER_ID] took ages".


def redact_text(text: str) -> str:
    if not text:
        return text
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _ORDER_ID_RE.sub("[ORDER_ID]", text)
    text = _PHONE_RE.sub(lambda m: "[PHONE]" if sum(c.isdigit() for c in m.group()) >= 8 else m.group(), text)
    return text


def hash_author(author_raw: str | None, source: str) -> str:
    """Stable per-source author hash. Same person across sources is not
    linkable by design (different account systems anyway)."""
    if not author_raw:
        author_raw = ""
    h = hashlib.sha256(f"{_SALT}:{source}:{author_raw}".encode("utf-8")).hexdigest()
    return h[:16]
