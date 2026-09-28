"""Shared scraping helpers: polite delay, session with UA, raw-response archiving."""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

import requests

from fh_feedback.config import CFG

_last_request_at: dict[str, float] = {}


def polite_wait(bucket: str = "default") -> None:
    """Sleep a random 3-6s (config-driven) since the last request in this bucket."""
    lo = CFG["scraping"]["min_delay_seconds"]
    hi = CFG["scraping"]["max_delay_seconds"]
    now = time.monotonic()
    last = _last_request_at.get(bucket)
    if last is not None:
        elapsed = now - last
        wait = random.uniform(lo, hi) - elapsed
        if wait > 0:
            time.sleep(wait)
    _last_request_at[bucket] = time.monotonic()


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": CFG["scraping"]["user_agent"],
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }
    )
    return s


def archive_raw(raw_dir: Path, name: str, payload: Any) -> Path:
    """Write raw payload (str or JSON-able object) to disk, unmodified, for reprocessing."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = raw_dir / name
    if isinstance(payload, (dict, list)):
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        out.write_text(str(payload), encoding="utf-8")
    return out
