"""Scrape the Apple App Store public customer-reviews JSON feed for the UK storefront.

Apple exposes recent reviews at:
    https://itunes.apple.com/{storefront}/rss/customerreviews/page={n}/id={app_id}/sortby=mostrecent/json
This is a public, unauthenticated feed (no login, no ToS-restricted scraping) but it
only returns roughly the most recent ~500 reviews (10/page, ~50 pages) — see caveats
in the report. We page until empty / cap / older than the window.

`parse_page()` is the reusable extraction logic: both `run()` (live scrape)
and `fh_feedback.normalise` (reprocessing archived raw JSON, no re-scrape)
call it, so there is exactly one place that understands this feed's shape.
"""
from __future__ import annotations

import datetime as dt
import logging

from fh_feedback.config import CFG, raw_dir, window_start
from fh_feedback.scrape.util import archive_raw, polite_wait, session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("appstore")

MAX_PAGES = 60  # hard cap; Apple stops returning entries well before this


def feed_url(storefront: str, app_id: str, page: int) -> str:
    return (
        f"https://itunes.apple.com/{storefront}/rss/customerreviews/"
        f"page={page}/id={app_id}/sortby=mostrecent/json"
    )


def parse_page(data: dict, storefront: str, app_id: str, persona: str) -> list[dict]:
    """Extract review records from one raw feed-page JSON payload. No window
    filtering here — that's applied once, consistently, wherever records are
    consumed (run()'s stop-paging logic, or normalise.py)."""
    entries = data.get("feed", {}).get("entry", [])
    out = []
    for e in entries:
        if "im:rating" not in e or "content" not in e:
            continue  # first entry is often app metadata, not a review
        out.append(
            {
                "source": "app_store",
                "native_id": e.get("id", {}).get("label"),
                "source_url": f"https://apps.apple.com/{storefront}/app/id{app_id}",
                "author_raw": e.get("author", {}).get("name", {}).get("label", ""),
                "rating": int(e["im:rating"]["label"]),
                "title": e.get("title", {}).get("label", ""),
                "text": e.get("content", {}).get("label", ""),
                "created_at": e.get("updated", {}).get("label"),  # ISO8601
                "app_version": e.get("im:version", {}).get("label"),
                "reviewer_country": "GB",
                "language": "en",
                "persona_hint": persona,
            }
        )
    return out


def run() -> list[dict]:
    cfg = CFG["sources"]["app_store"]
    storefront, app_id = cfg["storefront"], cfg["app_id"]
    persona = cfg["persona"]
    start = window_start()
    out_dir = raw_dir("app_store")
    s = session()

    results: list[dict] = []
    oldest_seen: dt.date | None = None
    for page in range(1, MAX_PAGES + 1):
        url = feed_url(storefront, app_id, page)
        polite_wait("appstore")
        resp = s.get(url, timeout=20)
        if resp.status_code != 200:
            log.warning("page %d: HTTP %s, stopping", page, resp.status_code)
            break
        try:
            data = resp.json()
        except ValueError:
            log.warning("page %d: non-JSON response, stopping", page)
            break

        archive_raw(out_dir, f"page_{page:03d}.json", data)

        records = parse_page(data, storefront, app_id, persona)
        if not records:
            log.info("page %d: no entries, stopping", page)
            break

        page_reviews = 0
        for rec in records:
            created_at = (
                dt.datetime.fromisoformat(rec["created_at"]).date() if rec["created_at"] else None
            )
            if created_at:
                if oldest_seen is None or created_at < oldest_seen:
                    oldest_seen = created_at
                if created_at < start:
                    continue  # outside window, but keep paging other entries on this page
            results.append(rec)
            page_reviews += 1

        log.info("page %d: %d reviews (oldest so far %s)", page, page_reviews, oldest_seen)

        if oldest_seen and oldest_seen < start:
            log.info("reached window start (%s), stopping", start)
            break

    log.info(
        "app_store: %d reviews collected, actual date range: %s to %s (window start requested: %s)",
        len(results),
        min((r["created_at"] for r in results), default=None),
        max((r["created_at"] for r in results), default=None),
        start,
    )
    return results


if __name__ == "__main__":
    run()
