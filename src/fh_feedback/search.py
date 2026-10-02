"""Hybrid retrieval CLI for the ask-feedback skill: SQL tag/date/source
filters, then semantic top-k within the filtered set via brute-force cosine
similarity (fine at this volume — see project plan Q19/Q23). Prints a JSON
array of hits with review_id, quote and metadata so answers can cite
sources exactly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

from fh_feedback.config import CFG
from fh_feedback.db import connect

# Avoid a network round-trip to the Hugging Face Hub on every load (the model
# is already cached locally from embed.py) — this alone was adding real
# latency on top of the reload itself.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

_model_cache: dict[str, object] = {}


def _get_model():
    """Load the embedding model once per process and reuse it. Loading it
    fresh on every call (the original behaviour) cost 6-10s per query —
    fine for a one-shot CLI invocation, a real problem for a server that
    calls this on every chat message."""
    name = CFG["embedding"]["model"]
    if name not in _model_cache:
        from sentence_transformers import SentenceTransformer

        _model_cache[name] = SentenceTransformer(name)
    return _model_cache[name]


def preload_model() -> None:
    """Call once at process/server startup so the first real request isn't
    the one that pays the load cost."""
    _get_model()


def _embed_query(query: str) -> np.ndarray:
    model = _get_model()
    vec = model.encode([query], normalize_embeddings=True)[0]
    return vec


def _build_filters(
    aspect: str | None = None,
    sentiment: str | None = None,
    persona: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
) -> tuple[str, list[str], list]:
    """Shared WHERE/JOIN builder for search() and count() - keeping these in
    one place means a count is guaranteed to reflect exactly the same rows
    search() ranks from, never a subtly different filter."""
    where = ["1=1"]
    params: list = []
    joins = ""
    if aspect or sentiment is not None:
        joins = "JOIN aspects a ON a.review_id = r.review_id"
        if aspect:
            where.append("a.aspect = ?")
            params.append(aspect)
        if sentiment:
            where.append("a.sentiment = ?")
            params.append(sentiment)
    if persona:
        joins2 = " LEFT JOIN tags t ON t.review_id = r.review_id" if "tags t" not in joins else ""
        joins += joins2
        where.append("t.persona = ?")
        params.append(persona)
    elif "tags t" not in joins and sentiment and not aspect:
        # overall-sentiment-only filter needs tags too
        joins += " LEFT JOIN tags t ON t.review_id = r.review_id"

    if source:
        where.append("r.source = ?")
        params.append(source)
    if since:
        where.append("r.created_at >= ?")
        params.append(since)
    if until:
        where.append("r.created_at <= ?")
        params.append(until)
    if min_rating:
        where.append("r.rating >= ?")
        params.append(min_rating)
    if max_rating:
        where.append("r.rating <= ?")
        params.append(max_rating)

    return joins, where, params


def count(
    aspect: str | None = None,
    sentiment: str | None = None,
    persona: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
) -> int:
    """Exact count of reviews matching the same filters search() accepts,
    ignoring top_k truncation and semantic ranking entirely. search() only
    ever returns its top_k by relevance score, which silently undercounts a
    narrow slice (e.g. "how many negative reviews in September") - callers
    that need a true total should use this instead of len(search(...))."""
    con = connect()
    joins, where, params = _build_filters(aspect, sentiment, persona, source, since, until, min_rating, max_rating)
    sql = f"SELECT COUNT(DISTINCT r.review_id) FROM reviews r {joins} WHERE {' AND '.join(where)}"
    n = con.execute(sql, params).fetchone()[0]
    con.close()
    return int(n)


def search(
    query: str | None,
    aspect: str | None = None,
    sentiment: str | None = None,
    persona: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    top_k: int = 15,
) -> list[dict]:
    con = connect()

    joins, where, params = _build_filters(aspect, sentiment, persona, source, since, until, min_rating, max_rating)

    # Always available for display (e.g. the chat UI's Sources panel), even
    # when no persona/sentiment filter already pulled tags in.
    if "tags t" not in joins:
        joins += " LEFT JOIN tags t ON t.review_id = r.review_id"

    sql = f"""
        SELECT DISTINCT r.review_id, r.source, r.source_url, r.rating, r.title, r.text,
               r.created_at, t.overall_sentiment, r.embedding
        FROM reviews r
        {joins}
        WHERE {' AND '.join(where)}
    """
    rows = con.execute(sql, params).fetchall()
    con.close()

    cols = ["review_id", "source", "source_url", "rating", "title", "text", "created_at", "overall_sentiment", "embedding"]
    hits = [dict(zip(cols, row)) for row in rows]

    if query:
        qvec = _embed_query(query)
        for h in hits:
            emb = h.pop("embedding")
            h["score"] = float(np.dot(qvec, emb)) if emb is not None else 0.0
        hits.sort(key=lambda h: h["score"], reverse=True)
    else:
        for h in hits:
            h.pop("embedding", None)
            h["score"] = None
        hits.sort(key=lambda h: h["created_at"] or "", reverse=True)

    for h in hits:
        h["created_at"] = h["created_at"].isoformat() if h["created_at"] else None

    return hits[:top_k]


def main() -> None:
    p = argparse.ArgumentParser(description="Search tagged Foodhub feedback")
    p.add_argument("query", nargs="?", help="free-text semantic query (optional if only filtering)")
    p.add_argument("--aspect")
    p.add_argument("--sentiment", choices=["positive", "negative", "neutral"])
    p.add_argument("--persona", choices=["diner", "partner", "unknown"])
    p.add_argument("--source", choices=["app_store", "google_play", "trustpilot", "reddit"])
    p.add_argument("--since", help="ISO date, e.g. 2026-06-01")
    p.add_argument("--until", help="ISO date")
    p.add_argument("--min-rating", type=int)
    p.add_argument("--max-rating", type=int)
    p.add_argument("--top-k", type=int, default=15)
    args = p.parse_args()

    hits = search(
        args.query,
        aspect=args.aspect,
        sentiment=args.sentiment,
        persona=args.persona,
        source=args.source,
        since=args.since,
        until=args.until,
        min_rating=args.min_rating,
        max_rating=args.max_rating,
        top_k=args.top_k,
    )
    json.dump(hits, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
