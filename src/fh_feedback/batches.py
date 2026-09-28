"""Export untagged reviews as JSONL batches for Claude Code to tag.

Each batch is a plain JSONL file of {review_id, rating, title, text,
created_at, source} — no PII beyond what's already redacted in `reviews`.
A subagent loads the `feedback-tagging` skill, reads one batch file, and
writes one matching tags file to data/tags/<same_stem>.json (a JSON array
of the schema in feedback-tagging/schema.json) for `ingest_tags.py` to load.
"""
from __future__ import annotations

import json
import logging

from fh_feedback.config import CFG, path_for
from fh_feedback.db import connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("batches")


def run() -> list[str]:
    batch_size = CFG["tagging"]["batch_size"]
    out_dir = path_for("batches_dir")
    con = connect()
    rows = con.execute(
        """
        SELECT r.review_id, r.source, r.rating, r.title, r.text, r.created_at
        FROM reviews r
        LEFT JOIN tags t ON t.review_id = r.review_id
        WHERE t.review_id IS NULL
        ORDER BY r.review_id
        """
    ).fetchall()
    cols = ["review_id", "source", "rating", "title", "text", "created_at"]
    con.close()

    written = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        stem = f"batch_{i // batch_size:04d}"
        fp = out_dir / f"{stem}.jsonl"
        with open(fp, "w", encoding="utf-8") as f:
            for row in chunk:
                rec = dict(zip(cols, row))
                rec["created_at"] = rec["created_at"].isoformat() if rec["created_at"] else None
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        written.append(str(fp))

    log.info("batches: %d untagged reviews -> %d batch files in %s", len(rows), len(written), out_dir)
    return written


if __name__ == "__main__":
    for p in run():
        print(p)
