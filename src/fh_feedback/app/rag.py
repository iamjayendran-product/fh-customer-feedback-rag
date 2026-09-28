"""RAG pipeline for the Feedback Radar chat app.

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

import hashlib
import json
import logging
import os
import re
import subprocess
import time

from fh_feedback import analyse
from fh_feedback.search import search

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
    "You are Feedback Radar. Answer in 2-3 short sentences MAXIMUM - a plain "
    "summary of the finding, nothing more. The UI shows full source quotes "
    "separately, so do not quote reviews at length or list review_ids in your "
    "prose; name a review_id only if you must point at one single example. "
    "Use only the data given in the prompt - never invent a number or finding. "
    "If nothing relevant was retrieved, say so in one sentence."
)

# ---------------------------------------------------------------------------
# Live aggregate stats. This used to be a hand-copied literal string (see
# CLAUDE.md) that would have silently gone stale the moment the underlying
# data changed - re-tagging, new scrapes, a taxonomy revision. Now computed
# from a live fh_feedback.analyse.run() call, cached briefly (it's a <1s
# query against 914 rows, but no need to re-run it on every message).
# ---------------------------------------------------------------------------
_stats_cache: dict = {"text": None, "at": 0.0}
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

    lines = [f"AGGREGATE STATS (live from feedback.duckdb, computed just now, {total} UK reviews):"]
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


def get_stats_briefing() -> str:
    now = time.time()
    if _stats_cache["text"] is None or now - _stats_cache["at"] > _STATS_TTL_S:
        try:
            data = analyse.run()
            _stats_cache["text"] = _format_stats(data)
            _stats_cache["at"] = now
        except Exception:
            log.exception("failed to refresh live stats - serving last known good copy")
            if _stats_cache["text"] is None:
                _stats_cache["text"] = "AGGREGATE STATS: unavailable right now (analyse.run() failed)."
    return _stats_cache["text"] + "\n\n" + STATIC_FINDINGS


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


_NEG_HINTS = ("complain", "problem", "issue", "negative", "bad ", "hate", "worst", "frustrat", "hurt", "angry")
_POS_HINTS = ("love", "praise", "positive", "best", "great", "happy", "delight")


def _guess_sentiment(question: str) -> str | None:
    q = question.lower()
    if any(h in q for h in _NEG_HINTS):
        return "negative"
    if any(h in q for h in _POS_HINTS):
        return "positive"
    return None


def retrieve(question: str, top_k: int = 15) -> list[dict]:
    """Semantic + light-heuristic retrieval over the tagged corpus."""
    sentiment = _guess_sentiment(question)
    hits = search(question, sentiment=sentiment, top_k=top_k)
    if not hits and sentiment:
        # sentiment guess may have over-narrowed; retry without it
        hits = search(question, top_k=top_k)
    return hits


def _format_evidence(hits: list[dict]) -> str:
    if not hits:
        return "RETRIEVED EVIDENCE: none matched this question."
    lines = ["RETRIEVED EVIDENCE (cite review_id for anything you use from here):"]
    for h in hits:
        lines.append(
            f"- review_id={h['review_id']} | source={h['source']} | rating={h.get('rating')} | "
            f"date={h.get('created_at')} | score={h.get('score')}\n  text: {h['text']}"
        )
    return "\n".join(lines)


def _verify_citations(text: str, hits: list[dict]) -> None:
    """RAG-quality guard: log (don't block on) any review_id-shaped token in
    the answer that isn't actually among the retrieved sources - a cheap
    tripwire for citation drift/hallucination, not a full quote-substring
    check (see CLAUDE.md for that known gap)."""
    known = {h["review_id"] for h in hits}
    cited = set(re.findall(r"\b(?:app_store|google_play):[0-9a-f-]{8,36}\b", text))
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


def _messages_for_api(question: str, history: list[dict], hits: list[dict]) -> list[dict]:
    messages = []
    for turn in history:
        role = "user" if turn.get("role") == "user" else "assistant"
        messages.append({"role": role, "content": turn.get("content", "")})
    messages.append(
        {
            "role": "user",
            "content": get_stats_briefing() + "\n\n" + _format_evidence(hits) + f"\n\nQuestion: {question}",
        }
    )
    return messages


def _stream_deltas_api(question: str, history: list[dict], hits: list[dict]):
    """Yields {'type':'delta','text':...}. No forced thinking phase - a
    plain Messages API call only 'thinks' if you explicitly ask it to."""
    try:
        client = _get_anthropic_client()
        with client.messages.stream(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=_messages_for_api(question, history, hits),
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
def _build_prompt_text(question: str, history: list[dict], hits: list[dict]) -> str:
    history_text = "\n".join(
        f"{'User' if t.get('role') == 'user' else 'Assistant'}: {t.get('content', '')}" for t in history
    )
    return "\n\n".join(
        [
            get_stats_briefing(),
            _format_evidence(hits),
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


def _stream_deltas_cli(question: str, history: list[dict], hits: list[dict]):
    """Yields {'type':'delta','text':...}, filtering out the model's
    internal 'thinking' tokens - only real answer text goes to the viewer."""
    prompt = _build_prompt_text(question, history, hits)
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
    hits = retrieve(question)
    yield {"type": "sources", "sources": hits}

    key = _cache_key(question, history)
    cached = _answer_cache.get(key)
    if cached and time.time() - cached["at"] < _CACHE_TTL_S:
        yield {"type": "done", "answer": cached["answer"], "sources": hits, "cached": True}
        return

    backend = _stream_deltas_api if GENERATION_BACKEND == "api" else _stream_deltas_cli
    full_text = []
    for event in backend(question, history, hits):
        full_text.append(event["text"])
        yield event

    final = "".join(full_text).strip() or "No answer came back."
    _verify_citations(final, hits)
    _answer_cache[key] = {"answer": final, "at": time.time()}
    yield {"type": "done", "answer": final, "sources": hits}


def answer(question: str, history: list[dict]) -> dict:
    """Non-streaming convenience wrapper, for scripts/tests."""
    events = list(stream_answer(question, history))
    done = next(e for e in reversed(events) if e["type"] == "done")
    return {"answer": done["answer"], "sources": done["sources"]}
