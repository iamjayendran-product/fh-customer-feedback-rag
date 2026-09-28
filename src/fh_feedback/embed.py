"""Embed review text with a local sentence-transformers model and store the
vectors directly in the `reviews` table (no separate vector store — see
project plan Q23: brute-force cosine over ~5-15k rows is fast enough)."""
from __future__ import annotations

import logging

from sentence_transformers import SentenceTransformer

from fh_feedback.config import CFG
from fh_feedback.db import connect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("embed")


def run(batch_size: int = 64) -> int:
    model_name = CFG["embedding"]["model"]
    model = SentenceTransformer(model_name)

    con = connect()
    rows = con.execute(
        "SELECT review_id, title, text FROM reviews WHERE embedding IS NULL"
    ).fetchall()
    log.info("embedding %d reviews with %s", len(rows), model_name)

    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        texts = [f"{r[1]}\n{r[2]}".strip() if r[1] else r[2] for r in chunk]
        vecs = model.encode(texts, normalize_embeddings=True)
        for (review_id, _, _), vec in zip(chunk, vecs):
            con.execute(
                "UPDATE reviews SET embedding = ? WHERE review_id = ?",
                [vec.tolist(), review_id],
            )
        log.info("embedded %d/%d", min(i + batch_size, len(rows)), len(rows))

    con.close()
    return len(rows)


if __name__ == "__main__":
    run()
