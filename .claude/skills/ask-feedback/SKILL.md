---
name: ask-feedback
description: Answer a question about Foodhub UK customer feedback by querying the tagged review corpus (DuckDB) and citing real review IDs. Use when Jay asks a discovery/research question about what customers say, e.g. "what do users say about refunds?" or "is the payment issue getting worse?".
---

# Ask feedback (RAG over the tagged corpus)

This is retrieval + citation, not free recall — every claim must trace to
specific `review_id`s returned by the search tool, never to general
knowledge about Foodhub or takeaway apps.

## Steps

1. Turn the question into a `fh_feedback.search` call. Activate venv first:
   `source .venv/bin/activate` (needed for the embedding model / DuckDB).
   Combine filters with a free-text query where it helps:
   ```
   python -m fh_feedback.search "refund taking too long" --aspect "Refunds & Complaint Handling" --sentiment negative --since 2026-04-01
   ```
   - Use `--aspect` when the question names a product area from
     `.claude/skills/feedback-tagging/taxonomy.md`.
   - Use `--sentiment`, `--persona`, `--source`, `--since`/`--until`,
     `--min-rating`/`--max-rating` to narrow before ranking semantically.
   - Omit the free-text query entirely for a pure filter ("show me all
     partner reviews this month") — results then sort by recency.
   - If the first call returns too few/irrelevant hits, loosen a filter
     (e.g. drop `--sentiment`) rather than guessing at different wording.
2. Read the returned `text` for each hit — don't just count them. A
   semantic match can still be off-topic; use judgement before citing it.
3. Answer with:
   - A direct answer to the question.
   - Supporting evidence as short quotes, each tagged with its
     `review_id` and `source` (e.g. *"the tracking is never right, just a
     guess" — google_play:5fdfc0ea...*).
   - Rough volume context if relevant ("this came up in N of the
     matched reviews") — but don't overstate precision; this is a v1
     corpus (App Store + Google Play only, UK, last 12 months — see
     `CLAUDE.md` for current source/window caveats).
4. If nothing relevant comes back, say so plainly rather than answering
   from general knowledge about Foodhub or the takeaway market.
