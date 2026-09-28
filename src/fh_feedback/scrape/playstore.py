"""Scrape Google Play reviews via the `google-play-scraper` library.

This library calls the same endpoints the Play Store web page uses (no login,
no private API key). We page newest-first and stop once we're past the
window start, archiving each raw page of results as returned by the library.

`parse_batch()` is the reusable extraction logic: both `run()` (live scrape)
and `fh_feedback.normalise` (reprocessing archived raw JSON, no re-scrape)
call it.
"""
from __future__ import annotations

import datetime as dt
import logging

from google_play_scraper import Sort, reviews

from fh_feedback.config import CFG, raw_dir, window_start
from fh_feedback.scrape.util import archive_raw, polite_wait

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("playstore")

PAGE_SIZE = 200
MAX_PAGES = 200  # hard cap as a safety net


def _serialize(batch: list[dict]) -> list[dict]:
    """Raw google-play-scraper dicts contain datetime objects; make them
    JSON-archivable without losing information."""
    return [
        {
            **r,
            "at": r["at"].isoformat() if r.get("at") else None,
            "repliedAt": r["repliedAt"].isoformat() if r.get("repliedAt") else None,
        }
        for r in batch
    ]


def parse_batch(batch: list[dict], app_id: str, country: str, lang: str, persona: str) -> list[dict]:
    """Extract review records from one raw batch (as returned by the library,
    or reloaded from an archived JSON page — same shape after `_serialize`)."""
    out = []
    for r in batch:
        at = r.get("at")
        if isinstance(at, str) and at:
            at_iso = at
        elif at:
            at_iso = at.isoformat()
        else:
            at_iso = None
        out.append(
            {
                "source": "google_play",
                "native_id": r["reviewId"],
                "source_url": f"https://play.google.com/store/apps/details?id={app_id}",
                "author_raw": r.get("userName", ""),
                "rating": r.get("score"),
                "title": "",
                "text": r.get("content", "") or "",
                "created_at": at_iso,
                "app_version": r.get("reviewCreatedVersion") or r.get("appVersion"),
                "reviewer_country": "GB",
                "language": lang,
                "persona_hint": persona,
            }
        )
    return out


def run() -> list[dict]:
    cfg = CFG["sources"]["google_play"]
    app_id, country, lang, persona = cfg["app_id"], cfg["country"], cfg["lang"], cfg["persona"]
    start = window_start()
    out_dir = raw_dir("google_play")

    results: list[dict] = []
    token = None
    oldest_seen: dt.date | None = None

    for page in range(1, MAX_PAGES + 1):
        polite_wait("playstore")
        batch, token = reviews(
            app_id,
            lang=lang,
            country=country,
            sort=Sort.NEWEST,
            count=PAGE_SIZE,
            continuation_token=token,
        )
        if not batch:
            log.info("page %d: empty batch, stopping", page)
            break

        archive_raw(out_dir, f"page_{page:03d}.json", _serialize(batch))

        records = parse_batch(batch, app_id, country, lang, persona)
        for rec in records:
            created = (
                dt.datetime.fromisoformat(rec["created_at"]).date() if rec["created_at"] else None
            )
            if created:
                if oldest_seen is None or created < oldest_seen:
                    oldest_seen = created
                if created < start:
                    continue
            results.append(rec)

        log.info("page %d: %d reviews (oldest so far %s)", page, len(records), oldest_seen)

        if not token or (oldest_seen and oldest_seen < start):
            log.info("stopping: token=%s oldest_seen=%s window_start=%s", bool(token), oldest_seen, start)
            break

    log.info(
        "google_play: %d reviews collected, date range %s to %s (window start %s)",
        len(results),
        min((r["created_at"] for r in results), default=None),
        max((r["created_at"] for r in results), default=None),
        start,
    )
    return results


if __name__ == "__main__":
    run()
