"""Aggregate stats over the tagged corpus: sentiment overview, top pain
themes ranked by volume x severity with a month-over-month trend, and
what's most praised. Prints JSON; the discovery report is written by hand
(Claude Code) from this output plus direct reading of quotes.
"""
from __future__ import annotations

import json
import sys

from fh_feedback.db import connect


def run() -> dict:
    con = connect()

    total_reviews = con.execute("SELECT count(*) FROM reviews").fetchone()[0]
    total_tagged = con.execute("SELECT count(*) FROM tags").fetchone()[0]

    overall_sentiment = con.execute(
        """
        SELECT r.source, t.overall_sentiment, count(*)
        FROM reviews r JOIN tags t ON t.review_id = r.review_id
        GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).fetchall()

    persona_counts = con.execute(
        "SELECT persona, count(*) FROM tags GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()

    mismatches = con.execute(
        "SELECT count(*) FROM tags WHERE star_text_mismatch = true"
    ).fetchone()[0]

    # Top pain themes: volume x severity for negative aspect tags, diner persona.
    pain_themes = con.execute(
        """
        SELECT a.aspect,
               count(*) AS n,
               avg(a.severity) AS avg_severity,
               count(*) * avg(a.severity) AS impact_score
        FROM aspects a
        JOIN tags t ON t.review_id = a.review_id
        WHERE a.sentiment = 'negative' AND t.persona != 'partner'
        GROUP BY 1
        ORDER BY impact_score DESC
        """
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
        """
        SELECT a.aspect, count(*) AS n
        FROM aspects a
        JOIN tags t ON t.review_id = a.review_id
        WHERE a.sentiment = 'positive' AND t.persona != 'partner'
        GROUP BY 1
        ORDER BY n DESC
        """
    ).fetchall()

    partner_summary = con.execute(
        """
        SELECT t.overall_sentiment, count(*)
        FROM tags t WHERE t.persona = 'partner'
        GROUP BY 1
        """
    ).fetchall()

    con.close()

    return {
        "total_reviews": total_reviews,
        "total_tagged": total_tagged,
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
