---
name: feedback-tagging
description: Tag one batch of Foodhub UK customer feedback (App Store / Google Play reviews) with persona, overall sentiment, and aspect-level sentiment against the frozen taxonomy. Use when asked to tag a batch file from data/batches/, or to label the gold set.
---

# Feedback tagging

Tags one JSONL batch of reviews (`data/batches/batch_XXXX.jsonl`, one JSON
object per line: `review_id, source, rating, title, text, created_at`)
against the taxonomy in `taxonomy.md`, using the exact output shape in
`schema.json`.

## Steps

1. Read `taxonomy.md` in this skill's folder (definitions, examples, edge
   cases) and `schema.json` (exact output shape + allowed aspect list) —
   every time, even if you've tagged before in this session. Don't rely on
   memory of a prior batch; the taxonomy is the source of truth.
2. Read the batch file. Each line is one review to tag independently —
   don't let one review's sentiment bleed into the next.
3. For each review, produce one record matching `schema.json`'s
   `record_shape` exactly:
   - Read the *whole* review, not just the star rating. Ratings and text
     sometimes disagree (see taxonomy.md's sarcasm and mismatch notes).
   - Assign `persona` from voice/content, not from the fact this is a
     consumer app store — partner voices do leak in (see taxonomy.md).
   - Assign zero, one, or several aspects. Don't force a match on a short
     or off-topic review.
   - Every `evidence_quote` must be a real substring of that review's
     `text` (or close paraphrase if the exact phrase is awkward to excerpt)
     — never invent a quote.
   - `severity` is only set when `sentiment: "negative"` for that aspect;
     otherwise `null`.
4. Write the array of records (same order as input, one record per input
   line) as a single JSON file to
   `data/tags/<same-stem-as-input>.json` (e.g. input
   `data/batches/batch_0003.jsonl` → output `data/tags/batch_0003.json`).
   Valid JSON array, UTF-8, no trailing commas.
5. Do not skip reviews. If a review is empty/unparseable, still emit a
   record with `is_foodhub: true, persona: "unknown", overall_sentiment:
   "neutral", star_text_mismatch: false, aspects: []` rather than omitting
   it — `ingest_tags.py` expects one output record per input review_id.

## When tagging the gold set instead of a batch

Same process, but the input is `data/gold/gold_set.csv` (columns:
`review_id, source, rating, title, text, created_at`, plus Jay's own
hand-labels in extra columns which you must ignore — don't peek at them
while tagging, or the accuracy check is meaningless). Output goes to
`data/gold/gold_set_predicted.json` in the same array shape. Tell the user
when this is a gold-set run vs a real batch, since the output path differs.

## Parallelising across many batches

Each batch file is independent — safe to dispatch one subagent per batch
file in parallel, each loading this skill fresh. Don't split a single
batch across agents; keep one agent's output internally consistent.
