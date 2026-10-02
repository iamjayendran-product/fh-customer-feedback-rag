# FH ReviewIQ — container image for a shared/hosted deployment.
# Local dev doesn't need this at all (see CLAUDE.md) - this is only for
# putting the app somewhere other people can reach.
FROM python:3.12-slim

WORKDIR /app

# System deps: none required beyond what the slim image ships - torch and
# sentence-transformers publish prebuilt manylinux wheels for this Python
# version, so no compiler toolchain is needed here (unlike this project's
# dev Mac - see CLAUDE.md's pyexpat note, which is a macOS-only issue and
# doesn't apply to this Linux base image at all).
COPY pyproject.toml ./
COPY src ./src
# CPU-only torch wheel first - sentence-transformers pulls torch, and pip's
# default index serves GPU/CUDA wheels on Linux (500MB+ of nvidia-* packages
# with no GPU here to use them). Installing the CPU wheel first satisfies
# the requirement before pip reaches for the GPU one.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -e .

# The tagged, embedded dataset - baked into the image as a read-only
# snapshot. Updating the data means rebuilding and redeploying the image
# (see CLAUDE.md "Updating the hosted dataset").
COPY data/processed/feedback.duckdb ./data/processed/feedback.duckdb
COPY config.yaml ./config.yaml

# Bake the embedding model into the image at build time (needs network
# here, at build time, only) so the container never needs network access
# to Hugging Face at runtime - matches HF_HUB_OFFLINE below.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

EXPOSE 8080
ENV PORT=8080

CMD ["python", "-m", "fh_feedback.app.server"]
