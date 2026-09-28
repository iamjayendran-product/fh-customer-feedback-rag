"""Scrape Reddit mentions of Foodhub via the public .json search endpoints
(no login, no API app registration needed for v1). Because "Foodhub" is a
generic-ish name, every result gets persona_hint="unknown" and an
is_foodhub_hint the tagging skill must confirm — never assume a keyword
match is actually about this Foodhub.
"""
from __future__ import annotations

import datetime as dt
import logging

from fh_feedback.config import CFG, raw_dir, window_start
from fh_feedback.scrape.util import archive_raw, polite_wait, session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reddit")

MAX_PAGES = 20


def run() -> list[dict]:
    cfg = CFG["sources"]["reddit"]
    query, subs = cfg["query"], cfg["subreddits"]
    start = window_start()
    out_dir = raw_dir("reddit")
    s = session()
    results: list[dict] = []

    for sub in subs:
        after = None
        for page in range(1, MAX_PAGES + 1):
            url = f"https://www.reddit.com/r/{sub}/search.json"
            params = {
                "q": query,
                "restrict_sr": "on",
                "sort": "new",
                "limit": 100,
                "t": "year",
            }
            if after:
                params["after"] = after
            polite_wait("reddit")
            resp = s.get(url, params=params, timeout=20)
            if resp.status_code != 200:
                log.warning("r/%s page %d: HTTP %s, stopping", sub, page, resp.status_code)
                break
            data = resp.json()
            archive_raw(out_dir, f"{sub}_page_{page:02d}.json", data)

            children = data.get("data", {}).get("children", [])
            if not children:
                break

            page_count = 0
            for c in children:
                p = c.get("data", {})
                created = dt.datetime.utcfromtimestamp(p["created_utc"]).date() if p.get("created_utc") else None
                if created and created < start:
                    continue
                text = (p.get("title", "") + "\n\n" + (p.get("selftext") or "")).strip()
                if not text:
                    continue
                results.append(
                    {
                        "source": "reddit",
                        "native_id": p.get("id"),
                        "source_url": f"https://reddit.com{p.get('permalink', '')}",
                        "author_raw": p.get("author", ""),
                        "rating": None,
                        "title": p.get("title", ""),
                        "text": text,
                        "created_at": dt.datetime.utcfromtimestamp(p["created_utc"]).isoformat() + "Z"
                        if p.get("created_utc")
                        else None,
                        "app_version": None,
                        "reviewer_country": None,  # unknown; subreddit choice is our UK proxy
                        "language": "en",
                        "persona_hint": "unknown",
                    }
                )
                page_count += 1

            log.info("r/%s page %d: %d posts kept", sub, page, page_count)

            after = data.get("data", {}).get("after")
            if not after:
                break

    log.info("reddit: %d posts collected across %d subreddits", len(results), len(subs))
    return results


if __name__ == "__main__":
    run()
