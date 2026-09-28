"""Export a rating-stratified sample of reviews as data/gold/gold_set.csv,
for Jay to hand-label before the tagging skill is trusted at scale (project
plan Q11). Adds empty gold_* columns to fill in; leaves everything else
untouched on re-export so partially-filled work isn't lost.
"""
from __future__ import annotations

import csv
import logging

from fh_feedback.config import CFG
from fh_feedback.db import connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gold")

GOLD_COLUMNS = ["gold_is_foodhub", "gold_persona", "gold_overall_sentiment", "gold_aspects"]
# gold_aspects format: "Aspect Name:sentiment|Aspect Name 2:sentiment2" (pipe-separated)


def run() -> str:
    n = CFG["tagging"]["gold_set_size"]
    from fh_feedback.config import ROOT

    out_path = ROOT / CFG["paths"]["gold_path"]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        log.warning("%s already exists — not overwriting a partially-labelled gold set", out_path)
        return str(out_path)

    con = connect()
    # Stratify by rating band (neg/mid/pos) and by source, so both app stores
    # and the full sentiment range are represented.
    rows = con.execute(
        f"""
        WITH banded AS (
            SELECT review_id, source, rating, title, text, created_at,
                CASE WHEN rating <= 2 THEN 'neg' WHEN rating = 3 THEN 'mid' ELSE 'pos' END AS band
            FROM reviews
        ), ranked AS (
            SELECT *, row_number() OVER (PARTITION BY band, source ORDER BY random()) AS rn
            FROM banded
        )
        SELECT review_id, source, rating, title, text, created_at
        FROM ranked
        WHERE rn <= {n}
        ORDER BY random()
        LIMIT {n}
        """
    ).fetchall()
    con.close()

    cols = ["review_id", "source", "rating", "title", "text", "created_at"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols + GOLD_COLUMNS)
        for row in rows:
            writer.writerow(list(row) + [""] * len(GOLD_COLUMNS))

    log.info("gold: wrote %d rows to %s — fill in the gold_* columns by hand", len(rows), out_path)
    return str(out_path)


if __name__ == "__main__":
    run()
