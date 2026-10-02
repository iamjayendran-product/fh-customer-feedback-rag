"""Aggregate stats over the tagged corpus: sentiment overview, top pain
themes ranked by volume x severity with a month-over-month trend, and
what's most praised. Prints JSON; the discovery report is written by hand
(Claude Code) from this output plus direct reading of quotes.
"""
from __future__ import annotations

import json
import sys

from fh_feedback.db import connect


def run(since: str | None = None, until: str | None = None) -> dict:
    """Aggregate stats. With since/until (ISO strings, same semantics as
    search.search), every figure is scoped to reviews created in that window;
    with neither, it covers the whole corpus. The trend is always all-time."""
    con = connect()

    # Every scoped query joins reviews as r; `w` is the shared window clause.
    conds, wp = [], []
    if since:
        conds.append("r.created_at >= ?")
        wp.append(since)
    if until:
        conds.append("r.created_at <= ?")
        wp.append(until)
    w = "".join(f" AND {c}" for c in conds)

    total_reviews = con.execute(f"SELECT count(*) FROM reviews r WHERE 1=1{w}", wp).fetchone()[0]
    total_tagged = con.execute(
        f"SELECT count(*) FROM tags t JOIN reviews r ON r.review_id = t.review_id WHERE 1=1{w}", wp
    ).fetchone()[0]
    min_date, max_date = con.execute(
        f"SELECT min(r.created_at), max(r.created_at) FROM reviews r WHERE 1=1{w}", wp
    ).fetchone()

    overall_sentiment = con.execute(
        f"""
        SELECT r.source, t.overall_sentiment, count(*)
        FROM reviews r JOIN tags t ON t.review_id = r.review_id
        WHERE 1=1{w}
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        wp,
    ).fetchall()

    persona_counts = con.execute(
        f"SELECT t.persona, count(*) FROM tags t JOIN reviews r ON r.review_id = t.review_id "
        f"WHERE 1=1{w} GROUP BY 1 ORDER BY 2 DESC",
        wp,
    ).fetchall()

    mismatches = con.execute(
        f"SELECT count(*) FROM tags t JOIN reviews r ON r.review_id = t.review_id "
        f"WHERE t.star_text_mismatch = true{w}",
        wp,
    ).fetchone()[0]

    # Top pain themes: volume x severity for negative aspect tags, diner persona.
    pain_themes = con.execute(
        f"""
        SELECT a.aspect,
               count(*) AS n,
               avg(a.severity) AS avg_severity,
               count(*) * avg(a.severity) AS impact_score
        FROM aspects a
        JOIN tags t ON t.review_id = a.review_id
        JOIN reviews r ON r.review_id = a.review_id
        WHERE a.sentiment = 'negative' AND t.persona != 'partner'{w}
        GROUP BY 1
        ORDER BY impact_score DESC
        """,
        wp,
    ).fetchall()

    # Trend: negative-aspect volume by month, for the top themes.
    trend = con.execute(
        """
        SELECT a.aspect, strftime(r.created_at, '%Y-%m') AS month, count(*)
        FROM aspects a
        JOIN reviews r ON r.review_id = a.review_id
        JOIN tags t ON t.review_id = a.review_id
        WHERE a.sentiment = 'negative' AND t.persona != 'partner'
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    ).fetchall()

    praise_themes = con.execute(
        f"""
        SELECT a.aspect, count(*) AS n
        FROM aspects a
        JOIN tags t ON t.review_id = a.review_id
        JOIN reviews r ON r.review_id = a.review_id
        WHERE a.sentiment = 'positive' AND t.persona != 'partner'{w}
        GROUP BY 1
        ORDER BY n DESC
        """,
        wp,
    ).fetchall()

    partner_summary = con.execute(
        f"""
        SELECT t.overall_sentiment, count(*)
        FROM tags t JOIN reviews r ON r.review_id = t.review_id
        WHERE t.persona = 'partner'{w}
        GROUP BY 1
        """,
        wp,
    ).fetchall()

    con.close()

    return {
        "total_reviews": total_reviews,
        "total_tagged": total_tagged,
        "date_range": {
            "min": min_date.isoformat() if min_date else None,
            "max": max_date.isoformat() if max_date else None,
        },
        "overall_sentiment_by_source": [
            {"source": s, "sentiment": sent, "count": c} for s, sent, c in overall_sentiment
        ],
        "persona_counts": [{"persona": p, "count": c} for p, c in persona_counts],
        "star_text_mismatches": mismatches,
        "pain_themes": [
            {"aspect": a, "n": n, "avg_severity": round(sev, 2) if sev else None, "impact_score": round(imp, 1)}
            for a, n, sev, imp in pain_themes
        ],
        "pain_theme_trend": [
            {"aspect": a, "month": m, "n": n} for a, m, n in trend
        ],
        "praise_themes": [{"aspect": a, "n": n} for a, n in praise_themes],
        "partner_overall_sentiment": [{"sentiment": s, "count": c} for s, c in partner_summary],
    }


if __name__ == "__main__":
    json.dump(run(), sys.stdout, ensure_ascii=False, indent=2)
