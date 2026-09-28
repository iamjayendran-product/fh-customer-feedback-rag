"""Turn archived raw scrape output into the standard `reviews` table.

Reads every archived raw page under data/raw/<source>/<any_date>/*, so this
can be re-run after a parser fix or a wider window without re-scraping
anything (the whole point of archiving raw responses). Applies the window
filter, PII redaction, author hashing and de-dup (by review_id) once, here,
so every downstream stage sees the same clean shape regardless of source.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from fh_feedback.config import CFG, ROOT, window_start
from fh_feedback.db import connect
from fh_feedback.redact import hash_author, redact_text
from fh_feedback.scrape.appstore import parse_page as parse_appstore_page
from fh_feedback.scrape.playstore import parse_batch as parse_playstore_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("normalise")


def _iter_raw_files(source: str, pattern: str) -> list[Path]:
    base = ROOT / CFG["paths"]["raw_dir"] / source
    if not base.exists():
        return []
    return sorted(base.glob(f"*/{pattern}"))  # */ = any run_date subfolder


def load_app_store() -> list[dict]:
    cfg = CFG["sources"]["app_store"]
    storefront, app_id, persona = cfg["storefront"], cfg["app_id"], cfg["persona"]
    out = []
    for fp in _iter_raw_files("app_store", "page_*.json"):
        data = json.loads(fp.read_text(encoding="utf-8"))
        out.extend(parse_appstore_page(data, storefront, app_id, persona))
    log.info("app_store: %d raw records from %d archived pages", len(out), len(_iter_raw_files("app_store", "page_*.json")))
    return out


def load_google_play() -> list[dict]:
    cfg = CFG["sources"]["google_play"]
    app_id, country, lang, persona = cfg["app_id"], cfg["country"], cfg["lang"], cfg["persona"]
    out = []
    files = _iter_raw_files("google_play", "page_*.json")
    for fp in files:
        batch = json.loads(fp.read_text(encoding="utf-8"))
        out.extend(parse_playstore_batch(batch, app_id, country, lang, persona))
    log.info("google_play: %d raw records from %d archived pages", len(out), len(files))
    return out


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def clean_and_load(raw_records: list[dict]) -> tuple[int, int]:
    """Redact, hash, filter to window, dedupe by review_id, upsert into DuckDB.
    Returns (rows_seen, rows_upserted)."""
    start = window_start()
    con = connect()
    now = dt.datetime.now(dt.timezone.utc)

    seen_ids = set()
    upserted = 0
    for rec in raw_records:
        created = _parse_dt(rec.get("created_at"))
        if created and created.date() < start:
            continue
        review_id = f"{rec['source']}:{rec['native_id']}"
        if review_id in seen_ids:
            continue  # de-dup within this run (same review re-appearing across pages)
        seen_ids.add(review_id)

        con.execute(
            """
            INSERT INTO reviews (
                review_id, source, source_url, author_hash, rating, title, text,
                created_at, app_version, reviewer_country, language, persona_hint,
                scraped_at, raw_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (review_id) DO UPDATE SET
                text = excluded.text,
                title = excluded.title,
                rating = excluded.rating
            """,
            [
                review_id,
                rec["source"],
                rec.get("source_url"),
                hash_author(rec.get("author_raw"), rec["source"]),
                rec.get("rating"),
                redact_text(rec.get("title") or ""),
                redact_text(rec.get("text") or ""),
                created,
                rec.get("app_version"),
                rec.get("reviewer_country"),
                rec.get("language"),
                rec.get("persona_hint"),
                now,
                None,
            ],
        )
        upserted += 1

    con.close()
    return len(raw_records), upserted


def run() -> None:
    all_records = load_app_store() + load_google_play()
    seen, upserted = clean_and_load(all_records)
    log.info("normalise: %d raw records seen, %d rows upserted into reviews", seen, upserted)


if __name__ == "__main__":
    run()
