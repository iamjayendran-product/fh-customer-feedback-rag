"""Scrape Trustpilot review pages by parsing the embedded __NEXT_DATA__ JSON
(server-rendered page state), rather than the visible HTML/DOM.

NOTE: this reads Trustpilot's public pages without logging in, but does not
use an official API, and Trustpilot's terms prohibit automated scraping.
This was an explicit, informed choice for v1 (see project plan) — kept
polite (3-6s between requests) and every raw page is archived so we never
need to re-hit the site just to re-parse or re-tag.

Covers both:
  - foodhub.co.uk  -> consumer reviews (persona: diner)
  - foodhub.com    -> "Foodhub For Business"          (persona: partner)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from fh_feedback.config import CFG, raw_dir, window_start
from fh_feedback.scrape.util import archive_raw, polite_wait, session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trustpilot")

MAX_PAGES = 200
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def _find_review_lists(node, path=""):
    """Recursively find lists of dicts that look like Trustpilot reviews.

    Resilient to Trustpilot's exact schema/version, which changes over time:
    we don't hardcode a JSON path, we look for the shape (a list whose items
    have both a rating-like and text-like field).
    """
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            found.extend(_find_review_lists(v, f"{path}.{k}"))
    elif isinstance(node, list) and node and isinstance(node[0], dict):
        keys = set(node[0].keys())
        if {"text"} <= keys and ({"rating"} & keys or {"consumer"} & keys):
            found.append(node)
        else:
            for item in node:
                found.extend(_find_review_lists(item, path + "[]"))
    return found


def parse_next_data(html: str) -> list[dict]:
    m = NEXT_DATA_RE.search(html)
    if not m:
        return []
    data = json.loads(m.group(1))
    lists = _find_review_lists(data)
    # Take the largest matching list — the real reviews array, as opposed to
    # incidental small lists that happen to share a key name.
    if not lists:
        return []
    return max(lists, key=len)


def _extract(review: dict, domain: str, persona: str) -> dict | None:
    text = review.get("text") or ""
    rating = review.get("rating")
    consumer = review.get("consumer") or {}
    dates = review.get("dates") or {}
    published = dates.get("publishedDate") or review.get("createdDateTime")
    native_id = review.get("id") or review.get("reviewId")
    if not native_id or not text:
        return None
    return {
        "source": "trustpilot",
        "native_id": f"{domain}:{native_id}",
        "source_url": f"https://uk.trustpilot.com/review/{domain}",
        "author_raw": consumer.get("displayName", ""),
        "rating": rating,
        "title": review.get("title", ""),
        "text": text,
        "created_at": published,
        "app_version": None,
        "reviewer_country": consumer.get("countryCode"),
        "language": review.get("language", "en"),
        "persona_hint": persona,
    }


def run_domain(domain: str, persona: str) -> list[dict]:
    start = window_start()
    out_dir = raw_dir("trustpilot")
    s = session()
    results: list[dict] = []
    oldest_seen: dt.date | None = None

    for page in range(1, MAX_PAGES + 1):
        url = f"https://uk.trustpilot.com/review/{domain}?page={page}&sort=recency"
        polite_wait("trustpilot")
        resp = s.get(url, timeout=20)
        if resp.status_code != 200:
            log.warning("%s page %d: HTTP %s, stopping", domain, page, resp.status_code)
            break

        archive_raw(out_dir, f"{domain}_page_{page:03d}.html", resp.text)

        reviews_raw = parse_next_data(resp.text)
        if not reviews_raw:
            log.info("%s page %d: no reviews found in __NEXT_DATA__, stopping", domain, page)
            break

        page_count = 0
        for r in reviews_raw:
            rec = _extract(r, domain, persona)
            if rec is None:
                continue
            created = None
            if rec["created_at"]:
                try:
                    created = dt.datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00")).date()
                except ValueError:
                    created = None
            if created:
                if oldest_seen is None or created < oldest_seen:
                    oldest_seen = created
                if created < start:
                    continue
            results.append(rec)
            page_count += 1

        log.info("%s page %d: %d reviews (oldest so far %s)", domain, page, page_count, oldest_seen)

        if oldest_seen and oldest_seen < start:
            log.info("%s: reached window start (%s), stopping", domain, start)
            break

    log.info(
        "trustpilot/%s: %d reviews collected, date range %s to %s (window start %s)",
        domain,
        len(results),
        min((r["created_at"] for r in results), default=None),
        max((r["created_at"] for r in results), default=None),
        start,
    )
    return results


def run() -> list[dict]:
    out: list[dict] = []
    for entry in CFG["sources"]["trustpilot"]["domains"]:
        out.extend(run_domain(entry["domain"], entry["persona"]))
    return out


if __name__ == "__main__":
    run()
