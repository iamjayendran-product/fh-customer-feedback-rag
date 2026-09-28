"""Compare the tagging skill's predictions on the gold set against Jay's
hand labels. Reports agreement on persona / overall_sentiment (simple %)
and on aspects (per-review Jaccard over {aspect, sentiment} pairs, then
averaged) — see project plan Q11 for the targets (>=85% / >=75%).
"""
from __future__ import annotations

import csv
import json
import logging
import sys

from fh_feedback.config import CFG, ROOT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eval")


def _parse_gold_aspects(raw: str) -> set[tuple[str, str]]:
    if not raw or not raw.strip():
        return set()
    out = set()
    for part in raw.split("|"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        aspect, sentiment = part.rsplit(":", 1)
        out.add((aspect.strip(), sentiment.strip()))
    return out


def load_gold(path) -> dict[str, dict]:
    gold = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("gold_persona", "").strip():
                continue  # not yet labelled — skip, don't count as a miss
            gold[row["review_id"]] = {
                "is_foodhub": row["gold_is_foodhub"].strip().lower() in ("true", "1", "yes"),
                "persona": row["gold_persona"].strip(),
                "overall_sentiment": row["gold_overall_sentiment"].strip(),
                "aspects": _parse_gold_aspects(row.get("gold_aspects", "")),
            }
    return gold


def load_predicted(path) -> dict[str, dict]:
    records = json.loads(open(path, encoding="utf-8").read())
    out = {}
    for r in records:
        out[r["review_id"]] = {
            "is_foodhub": r["is_foodhub"],
            "persona": r["persona"],
            "overall_sentiment": r["overall_sentiment"],
            "aspects": {(a["aspect"], a["sentiment"]) for a in r.get("aspects", [])},
        }
    return out


def run(gold_path=None, pred_path=None) -> dict:
    gold_path = gold_path or ROOT / CFG["paths"]["gold_path"]
    pred_path = pred_path or (ROOT / CFG["paths"]["gold_path"]).parent / "gold_set_predicted.json"

    gold = load_gold(gold_path)
    pred = load_predicted(pred_path)

    common = [rid for rid in gold if rid in pred]
    missing = [rid for rid in gold if rid not in pred]
    if missing:
        log.warning("%d gold rows have no prediction (missing from %s)", len(missing), pred_path)

    n = len(common)
    if n == 0:
        raise SystemExit("No overlapping labelled rows between gold and predicted — nothing to evaluate.")

    persona_agree = sum(1 for rid in common if gold[rid]["persona"] == pred[rid]["persona"])
    sentiment_agree = sum(1 for rid in common if gold[rid]["overall_sentiment"] == pred[rid]["overall_sentiment"])
    foodhub_agree = sum(1 for rid in common if gold[rid]["is_foodhub"] == pred[rid]["is_foodhub"])

    jaccards = []
    for rid in common:
        g, p = gold[rid]["aspects"], pred[rid]["aspects"]
        if not g and not p:
            jaccards.append(1.0)
            continue
        inter = len(g & p)
        union = len(g | p)
        jaccards.append(inter / union if union else 1.0)
    aspect_agreement = sum(jaccards) / len(jaccards)

    result = {
        "n_evaluated": n,
        "n_missing_predictions": len(missing),
        "persona_agreement": round(persona_agree / n, 3),
        "overall_sentiment_agreement": round(sentiment_agree / n, 3),
        "is_foodhub_agreement": round(foodhub_agree / n, 3),
        "aspect_agreement_avg_jaccard": round(aspect_agreement, 3),
        "targets": {"persona_and_overall": 0.85, "aspects": 0.75},
        "meets_targets": (
            persona_agree / n >= 0.85
            and sentiment_agree / n >= 0.85
            and aspect_agreement >= 0.75
        ),
    }
    return result


if __name__ == "__main__":
    json.dump(run(), sys.stdout, ensure_ascii=False, indent=2)
