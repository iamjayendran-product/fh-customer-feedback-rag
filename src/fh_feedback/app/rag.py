"""RAG pipeline for the FH ReviewIQ chat app.

Retrieval reuses fh_feedback.search's hybrid filter + embedding search - the
exact same engine the ask-feedback skill and the DuckDB analysis use.

Generation has two backends, chosen automatically by whether an API key is
set - see GENERATION_BACKEND below for why, and CLAUDE.md for the
measurements behind that choice:
  - "api" (ANTHROPIC_API_KEY set): calls the Anthropic API directly. No
    forced "thinking" phase, real per-request billing under a service
    identity - the only backend that works for a shared/hosted deployment
    with more than one user.
  - "cli" (no key set): shells out to a headless `claude -p` call, reusing
    this machine's own Claude Code login. No API key, no separate billing,
    but a ~2-10s mandatory "thinking" floor per message and fundamentally
    single-user - fine for local/personal use, not for a shared service.
"""
from __future__ import annotations

import calendar
import difflib
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from datetime import date, timedelta

from fh_feedback import analyse
from fh_feedback.search import count, search

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rag")

MODEL = "claude-haiku-4-5-20251001"

GENERATION_BACKEND = "api" if os.environ.get("ANTHROPIC_API_KEY") else "cli"
log.info("generation backend: %s", GENERATION_BACKEND)

# Answers capped short on purpose: with the "cli" backend, the model's own
# "thinking" phase (500-1000 tokens, unavoidable there) dominates latency
# far more than a few extra sentences of output do, but every sentence
# still costs real generation time either way, and the UI's Sources panel
# already carries the full quotes - repeating them in prose is pure waste.
SYSTEM_PROMPT = (
    "You are FH ReviewIQ, writing for an executive with a few SECONDS of attention "
    "- not a researcher who wants the full story in chat. The Sources panel the UI "
    "renders right below your answer already shows every underlying review in full "
    "(quote, source, rating, date), so your only job is the headline-level takeaway, "
    "never the supporting narrative - that's what Sources are for.\n\n"
    "Structure, always:\n"
    "1. One short opening line with the key number or finding (e.g. review count, "
    "top theme, a yes/no verdict).\n"
    "2. If there is more than one distinct point, follow with up to 4 bullet points, "
    "each its own line: '- **Short label**: 4-8 word takeaway.' One line per bullet, "
    "never a sentence that runs on past that.\n"
    "If there is genuinely only one point, skip the bullets and say it in 1-2 "
    "sentences total - do not force a list out of one idea.\n\n"
    "NEVER do any of the following: narrate a specific incident or scenario in prose "
    "(e.g. what one reviewer described happening to them) - the Sources quote already "
    "says it; write a bullet longer than one line; include a review_id (e.g. "
    "'app_store:123' or a google_play UUID) anywhere, even as an example - Sources "
    "already carries it; pad the answer with caveats or background nobody asked for.\n\n"
    "When the prompt contains a PERIOD STATS block, it covers EVERY review in the "
    "period the user asked about and is the authority on what is big or small there; "
    "the retrieved evidence is only a relevance-ranked sample. Rank themes by PERIOD "
    "STATS, say plainly when a period's counts are small, and never call something "
    "'isolated' or 'dominant' unless those counts support it. If the prompt says the "
    "period holds no reviews, say that and stop - do not substitute other periods.\n\n"
    "Use only the data given in the prompt - never invent a number or finding. If the "
    "question falls outside what the retrieved evidence and stats actually cover - e.g. "
    "a date outside the dataset's range, or nothing relevant retrieved - say so in ONE "
    "short sentence (max two) and stop there.\n\n"
    "A small GitHub-flavored-markdown table (header row + up to 5-6 rows, short cells, "
    "no review_id column) may replace the bullets ONLY for a literal side-by-side "
    "comparison (e.g. month-by-month numbers) - bullets are the default otherwise. "
    "Bold only a label or number, never a full sentence. No headings, block quotes, or "
    "nested lists."
)

# ---------------------------------------------------------------------------
# Live aggregate stats. This used to be a hand-copied literal string (see
# CLAUDE.md) that would have silently gone stale the moment the underlying
# data changed - re-tagging, new scrapes, a taxonomy revision. Now computed
# from a live fh_feedback.analyse.run() call, cached briefly (it's a <1s
# query against ~1K rows, but no need to re-run it on every message).
# ---------------------------------------------------------------------------
_stats_cache: dict = {"text": None, "at": 0.0, "max_date": None}
_STATS_TTL_S = 300

# Narrative findings from the manual root-cause deep dive (March 2026
# regression, chronic address bug, Restaurant Trust signal-vs-noise call) -
# these are conclusions from a one-time investigation, not raw counts, so
# they don't go stale the same way and aren't worth re-deriving live.
STATIC_FINDINGS = """Known findings from root-cause analysis (static, from a one-time investigation - re-verify if the underlying data has changed materially since):
- App Reliability spiked to 15 negative mentions in March 2026 (vs single digits other months). All 9 crash/checkout/payment-state bugs that month cluster Mar 7-31 - a likely release regression, not general wear.
- Address/location detection has been broken chronically for 8 months (Dec 2025-Jul 2026) with no clustering and no fix - a separate, standing bug.
- Restaurant Listings & Trust is low-volume (14-16 mentions) but highest-severity: six independent diners over 12 months describe restaurants marked open taking payment while actually closed. Both partner reviews in the dataset corroborate this from the business side. Read as signal, not noise.
No Trustpilot or Reddit data in this dataset (dropped for v1). No dedicated partner-voice channel (only 2 incidental partner reviews)."""


def _format_stats(d: dict) -> str:
    total = d["total_reviews"]
    by_sent: dict[str, int] = {}
    for row in d["overall_sentiment_by_source"]:
        by_sent[row["sentiment"]] = by_sent.get(row["sentiment"], 0) + row["count"]
    pos, neg, neu = by_sent.get("positive", 0), by_sent.get("negative", 0), by_sent.get("neutral", 0)

    date_range = d.get("date_range") or {}
    range_note = ""
    if date_range.get("min") and date_range.get("max"):
        range_note = f" Reviews span {date_range['min'][:10]} to {date_range['max'][:10]} - nothing exists outside that window."
    lines = [f"AGGREGATE STATS (live from feedback.duckdb, computed just now, {total} UK reviews).{range_note}"]
    if total:
        lines.append(
            f"Overall sentiment: {pos} positive, {neg} negative, {neu} neutral "
            f"({round(100*pos/total)}% / {round(100*neg/total)}% / {round(100*neu/total)}%)."
        )
    lines.append("Top pain themes (negative mentions, avg severity 1-3, impact = mentions x severity):")
    for i, row in enumerate(d["pain_themes"][:10], 1):
        lines.append(f"{i}. {row['aspect']} - {row['n']} mentions, severity {row['avg_severity']}, impact {row['impact_score']}")
    if d["praise_themes"]:
        lines.append(
            "Top praise themes (positive mentions): "
            + ", ".join(f"{row['aspect']} {row['n']}" for row in d["praise_themes"][:5])
        )
    lines.append(f"Star/text mismatches flagged: {d['star_text_mismatches']}.")
    lines.append(
        "Caveats to mention if asked about confidence/accuracy (not every answer): tagging was done "
        "by an LLM pipeline against a fixed taxonomy; treat exact figures as a strong first read, not "
        "a fully independently validated number, unless told otherwise."
    )
    return "\n".join(lines)


_period_cache: dict[tuple, dict] = {}


def _format_period_stats(d: dict, label: str, baseline: dict | None) -> str:
    """Stats for ONLY the requested window. Themes carry their share of the
    period's negative mentions next to the all-time share, so 'bigger or
    smaller than usual' is answerable even though the window's raw counts
    are on a different scale from the 12-month ones."""
    total = d["total_reviews"]
    dr = d.get("date_range") or {}
    span = f" ({dr['min'][:10]} to {dr['max'][:10]})" if dr.get("min") and dr.get("max") else ""
    by_sent: dict[str, int] = {}
    for row in d["overall_sentiment_by_source"]:
        by_sent[row["sentiment"]] = by_sent.get(row["sentiment"], 0) + row["count"]
    lines = [
        f"PERIOD STATS - {label}: {total} reviews{span}, covering EVERY review in that period "
        f"(not a sample). Overall sentiment: {by_sent.get('positive', 0)} positive, "
        f"{by_sent.get('negative', 0)} negative, {by_sent.get('neutral', 0)} neutral."
    ]
    neg_total = sum(r["n"] for r in d["pain_themes"])
    base_total = sum(r["n"] for r in baseline["pain_themes"]) if baseline else 0
    base = {r["aspect"]: r["n"] for r in baseline["pain_themes"]} if baseline else {}
    lines.append(
        f"Pain themes in this period ({neg_total} negative mentions total; severity 1-3; "
        "'usual share' = share of all negative mentions over the full 12 months):"
    )
    for i, r in enumerate(d["pain_themes"][:10], 1):
        usual = f", usual share {round(100 * base.get(r['aspect'], 0) / base_total)}%" if base_total else ""
        share = round(100 * r["n"] / neg_total) if neg_total else 0
        lines.append(
            f"{i}. {r['aspect']} - {r['n']} mentions ({share}% of period{usual}), "
            f"severity {r['avg_severity']}, impact {r['impact_score']}"
        )
    if not d["pain_themes"]:
        lines.append("(no negative aspect mentions in this period)")
    if d["praise_themes"]:
        lines.append(
            "Praise themes in this period: " + ", ".join(f"{r['aspect']} {r['n']}" for r in d["praise_themes"][:5])
        )
    return "\n".join(lines)


def get_period_stats(since: str, until: str, label: str) -> str:
    key = (since, until)
    hit = _period_cache.get(key)
    if hit and time.time() - hit["at"] < _STATS_TTL_S:
        return hit["text"]
    try:
        baseline = analyse.run()
        text = _format_period_stats(analyse.run(since=since, until=until), label, baseline)
    except Exception:
        log.exception("failed to compute period stats for %s", label)
        return ""
    _period_cache[key] = {"text": text, "at": time.time()}
    return text


def get_stats_briefing(period: dict | None = None) -> str:
    now = time.time()
    if _stats_cache["text"] is None or now - _stats_cache["at"] > _STATS_TTL_S:
        try:
            data = analyse.run()
            _stats_cache["text"] = _format_stats(data)
            _stats_cache["max_date"] = (data.get("date_range") or {}).get("max")
            _stats_cache["at"] = now
        except Exception:
            log.exception("failed to refresh live stats - serving last known good copy")
            if _stats_cache["text"] is None:
                _stats_cache["text"] = "AGGREGATE STATS: unavailable right now (analyse.run() failed)."
    out = _stats_cache["text"] + "\n\n" + STATIC_FINDINGS
    if period:
        ps = get_period_stats(period["since"], period["until"], period["label"])
        if ps:
            out = ps + "\n\n(Everything below is ALL-TIME context, 12 months - not the requested period.)\n" + out
    return out


def _dataset_max_date() -> str | None:
    get_stats_briefing()  # ensures _stats_cache is populated/fresh
    return _stats_cache.get("max_date")


# ---------------------------------------------------------------------------
# Answer cache: identical (question, history) pairs skip the LLM call
# entirely for 10 minutes. Retrieval (the "sources" event) still always
# runs fresh - it's cheap (~0.05-0.6s) and cost-free either backend.
# ---------------------------------------------------------------------------
_answer_cache: dict[str, dict] = {}
_CACHE_TTL_S = 600


def _cache_key(question: str, history: list[dict]) -> str:
    raw = json.dumps({"q": question, "h": history}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_NEG_HINTS = ("complain", "problem", "issue", "pain", "negative", "bad ", "hate", "worst", "frustrat", "hurt", "angry")
_POS_HINTS = ("love", "praise", "positive", "best", "great", "happy", "delight")


def _guess_sentiment(question: str) -> str | None:
    q = question.lower()
    if any(h in q for h in _NEG_HINTS):
        return "negative"
    if any(h in q for h in _POS_HINTS):
        return "positive"
    return None


# Month-name/period parsing for questions like "issues in September" or
# "last month" - without this, a month mention never became a date filter at
# all: retrieve() ran pure semantic search over the WHOLE 12-month corpus, so
# "September" only influenced ranking as much as the embedding happened to
# associate with that word (which is barely at all - reviews almost never
# name the month), and the Sources panel showed whatever ranked highest
# corpus-wide instead of anything actually from September.
_MONTH_LOOKUP = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTH_LOOKUP.update({abbr.lower(): i for i, abbr in enumerate(calendar.month_abbr) if abbr})
_MONTH_LOOKUP["sept"] = 9
_MONTH_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTH_LOOKUP, key=len, reverse=True)) + r")\b\.?\s*(\d{4})?",
    re.IGNORECASE,
)


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    last_day = calendar.monthrange(year, month)[1]
    since = date(year, month, 1).isoformat()
    # End-of-day on the last calendar day, not midnight - created_at is a
    # full timestamp, and "<= '2026-09-30'" would exclude anything later
    # than 00:00:00 that same day.
    until = f"{date(year, month, last_day).isoformat()} 23:59:59"
    return since, until


_NUM_WORDS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
}
_REL_RE = re.compile(
    r"\b(?:last|past|previous)\s+(\d{1,3}|" + "|".join(_NUM_WORDS) + r")\s+(day|week|month)s?\b"
)


def _fmt_day(d: date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


def _named_month(q: str) -> int | None:
    """Month number for a month word in q, tolerating typos ('septemeber')."""
    m = _MONTH_RE.search(q)
    if m:
        return _MONTH_LOOKUP[m.group(1).lower()]
    full = [n for n in _MONTH_LOOKUP if len(n) > 3]
    for word in re.findall(r"[a-z]{6,}", q):
        close = difflib.get_close_matches(word, full, n=1, cutoff=0.8)
        if close:
            return _MONTH_LOOKUP[close[0]]
    return None


def _relative_window(q: str, today: date) -> tuple[str, str, str] | None:
    """'last two weeks', 'past 30 days', 'last week', 'this week', 'this
    year' -> (since, until, label). Windows are anchored to the latest review
    in the dataset, not the system clock: the scrape lags the real date, so a
    clock-anchored 'last two weeks' can be partly or entirely empty even when
    the data is only a day or two behind."""
    latest = _dataset_max_date()
    anchor = min(today, date.fromisoformat(latest[:10])) if latest else today
    # "last two weeks OF September" counts back from that month's end, not
    # from the end of the dataset (they only coincide for the latest month).
    mo = _named_month(q) if re.search(r"\bof\s+[a-z]{3,}", q) else None
    if mo:
        year = anchor.year if mo <= anchor.month else anchor.year - 1
        anchor = min(anchor, date(year, mo, calendar.monthrange(year, mo)[1]))

    start: date | None = None
    m = _REL_RE.search(q)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS[m.group(1)]
        unit = m.group(2)
        if n < 1:
            return None
        if unit == "month":
            idx = anchor.year * 12 + anchor.month - 1 - n
            y, mo = divmod(idx, 12)
            start = date(y, mo + 1, min(anchor.day, calendar.monthrange(y, mo + 1)[1]))
        else:
            start = anchor - timedelta(days=n * (7 if unit == "week" else 1))
    elif re.search(r"\b(?:last|past) (?:week|fortnight)\b", q):
        start = anchor - timedelta(days=14 if "fortnight" in q else 7)
    elif re.search(r"\bthis week\b", q):
        start = anchor - timedelta(days=anchor.weekday())
    elif re.search(r"\bthis year\b", q):
        start = date(anchor.year, 1, 1)
    if start is None:
        return None
    return start.isoformat(), f"{anchor.isoformat()} 23:59:59", f"{_fmt_day(start)} to {_fmt_day(anchor)}"


def _extract_date_range(question: str, today: date | None = None) -> tuple[str | None, str | None, str | None]:
    """Best-effort parse of a month/period reference into a (since, until,
    label) filter. Returns (None, None, None) when the question names no
    specific period, in which case retrieval falls back to its old
    whole-corpus behaviour."""
    today = today or date.today()
    q = question.lower()

    rel = _relative_window(q, today)
    if rel:
        return rel

    if re.search(r"\blast month\b", q):
        prev_last_day = today.replace(day=1) - timedelta(days=1)
        since, until = _month_bounds(prev_last_day.year, prev_last_day.month)
        return since, until, prev_last_day.strftime("%B %Y")
    if re.search(r"\bthis month\b", q):
        since, until = _month_bounds(today.year, today.month)
        return since, until, today.strftime("%B %Y")
    if re.search(r"\bnext month\b", q):
        # Jump to day 28 then add 4 days to land in the following month
        # regardless of the current month's length, then snap to its 1st -
        # the standard trick for "add a month" without a calendar library.
        next_month_first = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        since, until = _month_bounds(next_month_first.year, next_month_first.month)
        return since, until, next_month_first.strftime("%B %Y")

    m = _MONTH_RE.search(q)
    if m:
        month = _MONTH_LOOKUP[m.group(1).lower()]
        # No year given ("September" alone): assume the most recent
        # occurrence that isn't in the future, not the current calendar
        # year blindly - asked in October about "September" should mean the
        # September just gone, whatever year that falls in.
        year = int(m.group(2)) if m.group(2) else (today.year if month <= today.month else today.year - 1)
        since, until = _month_bounds(year, month)
        return since, until, date(year, month, 1).strftime("%B %Y")

    return None, None, None


# A genuine "predict the future" question isn't a retrieval gap to apologize
# for - it's categorically impossible (no review of a thing that hasn't
# happened yet can exist), so it gets a canned, personality-driven reply
# instead of asking the LLM to improvise a serious-sounding non-answer.
FUTURE_RESPONSE = (
    "Hmm… smarty! \U0001F60F I can’t predict the future… yet. If I could, would you "
    "really be using me like this? \U0001F602 I’d hope you’d put my future-telling powers "
    "to better use! LOL."
)

_FUTURE_HINTS = (
    "predict", "forecast", "will there be", "will happen", "will improve", "will get better",
    "will get worse", "going forward", "what will", "upcoming", "future issue", "future problem",
    "next year", "will it", "going to happen", "going to improve", "going to get",
    "next week", "next quarter", "tomorrow",
)


def _is_future_question(question: str, since: str | None) -> bool:
    q = question.lower()
    if any(h in q for h in _FUTURE_HINTS):
        return True
    if since:
        # A parsed period starting after the last review in the dataset is
        # asking about something that hasn't happened yet, not a gap in
        # retrieval - distinct from _extract_date_range's "wrong year
        # guessed" fallback in retrieve(), which still refers to a real past
        # period.
        latest = _dataset_max_date()
        if latest and since > latest:
            return True
    return False


def retrieve(question: str, top_k: int = 15) -> tuple[list[dict], dict | None]:
    """Semantic + light-heuristic retrieval over the tagged corpus.

    Returns (hits, date_meta). date_meta is None unless the question named a
    specific month/period; when set, it carries the TRUE total count for
    that period+sentiment filter (via search.count()), since `hits` itself
    is only ever the top `top_k` by semantic score and silently undercounts
    a period with few matches otherwise."""
    sentiment = _guess_sentiment(question)
    since, until, label = _extract_date_range(question)

    search_top_k = max(top_k, 50) if label else top_k
    hits = search(question, sentiment=sentiment, since=since, until=until, top_k=search_top_k)
    if not hits and sentiment:
        # sentiment guess may have over-narrowed; retry without it
        hits = search(question, since=since, until=until, top_k=search_top_k)
        sentiment = None

    date_meta = None
    if label:
        # An empty period is reported as empty. Falling back to unfiltered
        # search here is what once made "last two weeks" answer from reviews
        # dated months earlier and conclude there was no data.
        total = count(sentiment=sentiment, since=since, until=until)
        date_meta = {"label": label, "total": total, "shown": len(hits), "since": since, "until": until}
    return hits, date_meta


def _format_evidence(hits: list[dict], date_meta: dict | None = None) -> str:
    if not hits:
        if date_meta:
            return (
                f"RETRIEVED EVIDENCE: the dataset holds no matching reviews for {date_meta['label']}. "
                "Say so in one sentence; do not answer from other periods."
            )
        return "RETRIEVED EVIDENCE: none matched this question."
    lines = []
    if date_meta:
        lines.append(
            f"NOTE: {date_meta['total']} review(s) in total match this question's filters for "
            f"{date_meta['label']} - that is the TRUE count, state that number if asked how many. "
            f"Showing {date_meta['shown']} below, ranked by relevance - a sample, so use PERIOD STATS "
            "(not this list) to judge which themes are large or small."
        )
    lines.append("RETRIEVED EVIDENCE (cite review_id for anything you use from here):")
    for h in hits:
        lines.append(
            f"- review_id={h['review_id']} | source={h['source']} | rating={h.get('rating')} | "
            f"date={h.get('created_at')} | score={h.get('score')}\n  text: {h['text']}"
        )
    return "\n".join(lines)


_REVIEW_ID_RE = re.compile(r"\b(?:app_store|google_play):[0-9a-f-]{8,36}\b", re.IGNORECASE)


def _strip_review_ids(text: str) -> str:
    """Hard guarantee that no review_id ever reaches the chat bubble, instead
    of relying solely on SYSTEM_PROMPT telling the model not to include one -
    a prompt instruction can occasionally be ignored, a regex scrub can't.
    Also tidies up whatever punctuation/parens the model wrapped the id in."""
    cleaned = _REVIEW_ID_RE.sub("", text)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)  # now-empty parens, e.g. "(app_store:123)" -> "()"
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)  # double spaces left by the removal
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)  # stray space before trailing punctuation
    return cleaned.strip()


def _verify_citations(text: str, hits: list[dict]) -> None:
    """RAG-quality guard: log (don't block on) any review_id-shaped token in
    the answer that isn't actually among the retrieved sources - a cheap
    tripwire for citation drift/hallucination, not a full quote-substring
    check (see CLAUDE.md for that known gap)."""
    known = {h["review_id"] for h in hits}
    cited = set(_REVIEW_ID_RE.findall(text))
    bogus = cited - known
    if bogus:
        log.warning("answer cited review_id(s) not in retrieved sources: %s", bogus)


# ---------------------------------------------------------------------------
# Backend: Anthropic API (shared/hosted deployments)
# ---------------------------------------------------------------------------
_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic

        _anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    return _anthropic_client


def _period(date_meta: dict | None) -> dict | None:
    if not date_meta:
        return None
    return {"since": date_meta["since"], "until": date_meta["until"], "label": date_meta["label"]}


def _messages_for_api(question: str, history: list[dict], hits: list[dict], date_meta: dict | None = None) -> list[dict]:
    messages = []
    for turn in history:
        role = "user" if turn.get("role") == "user" else "assistant"
        messages.append({"role": role, "content": turn.get("content", "")})
    messages.append(
        {
            "role": "user",
            "content": get_stats_briefing(_period(date_meta)) + "\n\n" + _format_evidence(hits, date_meta) + f"\n\nQuestion: {question}",
        }
    )
    return messages


def _stream_deltas_api(question: str, history: list[dict], hits: list[dict], date_meta: dict | None = None):
    """Yields {'type':'delta','text':...}. No forced thinking phase - a
    plain Messages API call only 'thinks' if you explicitly ask it to."""
    try:
        client = _get_anthropic_client()
        with client.messages.stream(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=_messages_for_api(question, history, hits, date_meta),
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield {"type": "delta", "text": text}
    except Exception as e:  # noqa: BLE001 - surfaced to the chat, not swallowed
        log.exception("Anthropic API call failed")
        yield {"type": "delta", "text": f"[Server error calling Claude: {e}]"}


# ---------------------------------------------------------------------------
# Backend: headless `claude -p` (local/personal use, no API key)
# ---------------------------------------------------------------------------
def _build_prompt_text(question: str, history: list[dict], hits: list[dict], date_meta: dict | None = None) -> str:
    history_text = "\n".join(
        f"{'User' if t.get('role') == 'user' else 'Assistant'}: {t.get('content', '')}" for t in history
    )
    return "\n\n".join(
        [
            get_stats_briefing(_period(date_meta)),
            _format_evidence(hits, date_meta),
            ("CONVERSATION SO FAR:\n" + history_text) if history else "",
            f"User: {question}\nAssistant:",
        ]
    ).strip()


def _claude_cmd(prompt: str) -> list[str]:
    return [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--model",
        MODEL,
        "--system-prompt",
        SYSTEM_PROMPT,
        "--tools",
        "",
        "--disable-slash-commands",
    ]


def _stream_deltas_cli(question: str, history: list[dict], hits: list[dict], date_meta: dict | None = None):
    """Yields {'type':'delta','text':...}, filtering out the model's
    internal 'thinking' tokens - only real answer text goes to the viewer."""
    prompt = _build_prompt_text(question, history, hits, date_meta)
    proc = subprocess.Popen(_claude_cmd(prompt), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    got_any = False
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "stream_event":
                inner = event.get("event", {})
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        got_any = True
                        yield {"type": "delta", "text": delta["text"]}
            elif event.get("type") == "result":
                if not got_any and event.get("result"):
                    yield {"type": "delta", "text": event["result"]}
                return
    finally:
        proc.wait(timeout=5)
        if proc.returncode not in (0, None) and not got_any:
            stderr = proc.stderr.read()[:2000] if proc.stderr else ""
            log.error("claude -p (stream) failed: %s", stderr)
            yield {"type": "delta", "text": "[Something went wrong answering that - check the server log.]"}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def stream_answer(question: str, history: list[dict]):
    """Generator yielding {'type':'sources','sources':...} as soon as
    retrieval finishes (real evidence on screen well under a second),
    then {'type':'delta','text':...} as the summary is written, then one
    final {'type':'done','answer':...,'sources':...}. Identical
    (question, history) pairs are cache-hit and skip the LLM call entirely.
    """
    since, _until, _label = _extract_date_range(question)
    if _is_future_question(question, since):
        yield {"type": "sources", "sources": []}
        yield {"type": "done", "answer": FUTURE_RESPONSE, "sources": []}
        return

    hits, date_meta = retrieve(question)
    yield {"type": "sources", "sources": hits}

    key = _cache_key(question, history)
    cached = _answer_cache.get(key)
    if cached and time.time() - cached["at"] < _CACHE_TTL_S:
        yield {"type": "done", "answer": cached["answer"], "sources": hits, "cached": True}
        return

    backend = _stream_deltas_api if GENERATION_BACKEND == "api" else _stream_deltas_cli
    full_text = []
    for event in backend(question, history, hits, date_meta):
        full_text.append(event["text"])
        yield event

    final = "".join(full_text).strip() or "No answer came back."
    final = _strip_review_ids(final)
    _verify_citations(final, hits)
    _answer_cache[key] = {"answer": final, "at": time.time()}
    yield {"type": "done", "answer": final, "sources": hits}


def answer(question: str, history: list[dict]) -> dict:
    """Non-streaming convenience wrapper, for scripts/tests."""
    events = list(stream_answer(question, history))
    done = next(e for e in reversed(events) if e["type"] == "done")
    return {"answer": done["answer"], "sources": done["sources"]}
