"""DuckDB schema and connection helper. One file holds reviews, tags, aspects
and (later) embeddings — no separate vector store needed at this volume."""
from __future__ import annotations

import duckdb

from fh_feedback.config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id         VARCHAR PRIMARY KEY,   -- "<source>:<native_id>"
    source            VARCHAR,               -- app_store | google_play | trustpilot | reddit
    source_url        VARCHAR,
    author_hash       VARCHAR,
    rating            INTEGER,               -- nullable (reddit posts have none)
    title             VARCHAR,
    text              VARCHAR,               -- redacted
    created_at        TIMESTAMP,
    app_version       VARCHAR,
    reviewer_country  VARCHAR,
    language          VARCHAR,
    persona_hint      VARCHAR,               -- from source config; skill confirms/overrides
    scraped_at        TIMESTAMP,
    raw_path          VARCHAR,
    embedding         DOUBLE[]               -- filled by embed.py
);

CREATE TABLE IF NOT EXISTS tags (
    -- Not a formal FK to reviews(review_id): DuckDB's FK enforcement blocks
    -- ordinary UPDATEs to the parent table once any child row references it
    -- (a documented DuckDB limitation, not just an edge case — hit this
    -- re-embedding reviews after 714+ rows had been tagged). Referential
    -- integrity here is enforced by ingest_tags.py instead (every review_id
    -- it writes comes from a real reviews.review_id).
    review_id           VARCHAR PRIMARY KEY,
    is_foodhub          BOOLEAN,
    persona             VARCHAR,             -- diner | partner | unknown
    overall_sentiment   VARCHAR,             -- positive | negative | neutral
    star_text_mismatch  BOOLEAN,
    skill_version       VARCHAR,
    tagged_at           TIMESTAMP
);

CREATE TABLE IF NOT EXISTS aspects (
    review_id       VARCHAR,  -- see tags.review_id comment: no formal FK
    aspect          VARCHAR,
    sentiment       VARCHAR,                -- positive | negative | neutral
    severity        INTEGER,                -- 1-3
    evidence_quote  VARCHAR
);
"""


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path()))
    con.execute(SCHEMA)
    return con
