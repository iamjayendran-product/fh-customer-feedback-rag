"""Validate tag JSON files against the feedback-tagging schema and load them
into the `tags` / `aspects` DuckDB tables."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from fh_feedback.config import CFG, path_for
from fh_feedback.db import connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest_tags")

SCHEMA_PATH = Path(__file__).resolve().parents[2] / ".claude/skills/feedback-tagging/schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(rec: dict, allowed_aspects: set[str]) -> list[str]:
    errors = []
    required = ["review_id", "is_foodhub", "persona", "overall_sentiment", "star_text_mismatch", "aspects"]
    for k in required:
        if k not in rec:
            errors.append(f"missing field {k}")
    if rec.get("persona") not in ("diner", "partner", "unknown"):
        errors.append(f"bad persona {rec.get('persona')!r}")
    if rec.get("overall_sentiment") not in ("positive", "negative", "neutral"):
        errors.append(f"bad overall_sentiment {rec.get('overall_sentiment')!r}")
    for a in rec.get("aspects", []):
        if a.get("aspect") not in allowed_aspects:
            errors.append(f"bad aspect {a.get('aspect')!r}")
        if a.get("sentiment") not in ("positive", "negative", "neutral"):
            errors.append(f"bad aspect sentiment {a.get('sentiment')!r}")
        sev = a.get("severity")
        if sev is not None and sev not in (1, 2, 3):
            errors.append(f"bad severity {sev!r}")
    return errors


def run(skill_version: str | None = None) -> tuple[int, int]:
    schema = _load_schema()
    skill_version = skill_version or schema["skill_version"]
    allowed_aspects = set(schema["aspects_allowed"])

    tags_dir = path_for("tags_dir")
    files = sorted(tags_dir.glob("*.json"))
    con = connect()
    now = dt.datetime.now(dt.timezone.utc)

    total, rejected = 0, 0
    for fp in files:
        records = json.loads(fp.read_text(encoding="utf-8"))
        for rec in records:
            total += 1
            errors = _validate(rec, allowed_aspects)
            if errors:
                rejected += 1
                log.warning("%s / %s: rejected: %s", fp.name, rec.get("review_id"), errors)
                continue

            con.execute(
                """
                INSERT INTO tags (review_id, is_foodhub, persona, overall_sentiment,
                                   star_text_mismatch, skill_version, tagged_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (review_id) DO UPDATE SET
                    is_foodhub = excluded.is_foodhub,
                    persona = excluded.persona,
                    overall_sentiment = excluded.overall_sentiment,
                    star_text_mismatch = excluded.star_text_mismatch,
                    skill_version = excluded.skill_version,
                    tagged_at = excluded.tagged_at
                """,
                [
                    rec["review_id"],
                    rec["is_foodhub"],
                    rec["persona"],
                    rec["overall_sentiment"],
                    rec["star_text_mismatch"],
                    skill_version,
                    now,
                ],
            )
            con.execute("DELETE FROM aspects WHERE review_id = ?", [rec["review_id"]])
            for a in rec.get("aspects", []):
                con.execute(
                    "INSERT INTO aspects (review_id, aspect, sentiment, severity, evidence_quote) VALUES (?, ?, ?, ?, ?)",
                    [rec["review_id"], a["aspect"], a["sentiment"], a.get("severity"), a.get("evidence_quote")],
                )

    con.close()
    log.info("ingest_tags: %d records seen across %d files, %d rejected", total, len(files), rejected)
    return total, rejected


if __name__ == "__main__":
    run()
