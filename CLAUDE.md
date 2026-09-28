# FH Customer Feedback — RAG (v1)

Research & discovery pipeline: collate public UK feedback about the Foodhub
marketplace (app + website) from the last 12 months, tag it for sentiment and
product-area, and produce a discovery report of **problems** (not solutions)
to feed the roadmap. See `~/.claude/plans/i-want-to-setup-crispy-walrus.md`
for the full decision record from the grilling session.

## Environment setup (do this first, every fresh clone/machine)

This Mac's Homebrew `python@3.14` has a broken `pyexpat` against macOS 26.2's
system libexpat (breaks pip, venv, anything touching XML/plistlib). Fix:

```bash
brew install expat   # keg-only, safe, won't touch the system one
python3 -m venv .venv --system-site-packages --without-pip
```

Then append to `.venv/bin/activate` (already done in this repo — check it's
still there before debugging a "mac_ver" or pip SSL error):

```bash
export DYLD_LIBRARY_PATH="/opt/homebrew/opt/expat/lib:$DYLD_LIBRARY_PATH"
```

```bash
source .venv/bin/activate
python3 -m pip install -e .
```

## Pipeline stages (run in order)

| # | Command | Output |
|---|---|---|
| 1 | `python -m fh_feedback.scrape.appstore` | `data/raw/app_store/<date>/*.json` |
| 1 | `python -m fh_feedback.scrape.playstore` | `data/raw/google_play/<date>/*.json` |
| 2 | `python -m fh_feedback.normalise` | `data/processed/feedback.duckdb` (`reviews` table) |
| 3 | `python -m fh_feedback.batches` | `data/batches/*.jsonl` (untagged, ~100/file) |
| 4 | Claude Code + `feedback-tagging` skill, one subagent per batch | `data/tags/*.json` |
| 5 | `python -m fh_feedback.ingest_tags` | loads `tags`/`aspects` tables |
| 6 | `python -m fh_feedback.embed` | fills `reviews.embedding` |
| 7 | `python -m fh_feedback.analyse` | aggregate stats for the report |
| 8 | `python -m fh_feedback.eval --gold data/gold/gold_set.csv` | agreement report vs gold set |
| — | `python -m fh_feedback.search "<query>" [--filters ...]` | JSON hits with review IDs, used by the `ask-feedback` skill |

All raw scrape output is archived under `data/raw/` — re-run stages 2+ without
re-scraping whenever the parser or taxonomy changes.

## Feedback Radar — local chat app

A standalone conversational RAG app, separate from Claude Code itself:

```bash
source .venv/bin/activate
python -m fh_feedback.app.server        # opens http://127.0.0.1:8765
```

- Retrieval: `fh_feedback.search` (real embeddings + DuckDB) — same engine
  the `ask-feedback` skill uses.
- Answer generation: a headless `claude -p` subprocess call per message
  (`src/fh_feedback/app/rag.py`) — no API key, reuses this machine's Claude
  Code login. Uses `--model claude-haiku-4-5-20251001 --system-prompt
  "..." --tools "" --disable-slash-commands` specifically: the *default*
  headless invocation (no overrides) carries the full Claude Code harness
  system prompt and costs $0.05-0.17 per call from ~30-40K cache-creation
  tokens; this stripped-down form costs roughly $0.005-0.02/message. Keep
  `--system-prompt` byte-identical across calls — that's most of why the
  cost stays low (prompt caching on the fixed portion).
- Frontend: plain HTML/CSS/JS at `src/fh_feedback/app/static/`, no build
  step. Chat history persists per-browser in `localStorage`.
- Known limitation: it can't answer month-by-month trend questions
  ("is this getting better or worse?") since retrieval returns individual
  reviews, not time-bucketed aggregates — it correctly says so rather than
  guessing. A `get_trend(aspect)` tool would close this gap if needed.
- **Performance**: several fixes applied after the first version felt slow.
  (1) `fh_feedback.search` now caches the embedding model process-wide
  (`preload_model()`, called once at server startup) instead of reloading
  it on every message — this alone was 6-10s/message, down to ~0.05-0.6s.
  (2) `/api/chat` streams as newline-delimited JSON over chunked transfer
  encoding: `{"type":"sources",...}` fires the instant retrieval finishes
  (well under 1s — real evidence on screen immediately), then
  `{"type":"delta","text":...}` as the summary is written, then one
  `{"type":"done",...}`. Uses `claude -p --output-format stream-json
  --include-partial-messages --verbose` (the `--verbose` flag is required
  alongside `--print` + `stream-json`, easy to miss).
  (3) Identical `(question, history)` pairs are cached 10 minutes
  (`_answer_cache` in `rag.py`) and skip the LLM call entirely.
  (4) `SYSTEM_PROMPT` caps answers at 2-3 sentences with no inline quotes
  (the Sources panel already carries them) — shorter output = less
  generation time, and matches what a chat answer should look like anyway.
  **Hard floor that remains, by design choice (2026-09-24):** `claude -p`
  always runs extended thinking (500-1000 tokens, ~2-10s, highly variable)
  before writing anything, with no CLI flag to disable it (`--effort low`
  was tested — made it worse, not better). A direct Anthropic API call
  with no `thinking` param would remove this entirely and was offered as
  an option; declined in favour of no new API key / no separate billing.
  If that trade-off is revisited later, that's the one change that would
  actually move the needle further — everything above is already the
  ceiling of what's fixable while still shelling out to `claude -p`.
- **Two generation backends**, auto-selected by `fh_feedback/app/rag.py`
  based on whether `ANTHROPIC_API_KEY` is set:
  - `cli` (no key): the `claude -p` approach above. Single-user by nature —
    every request runs under whoever's Claude Code login is on the host
    machine. Fine for local/personal use, not for sharing the URL.
  - `api` (key set): calls the Anthropic API directly (no `claude -p`,
    no CLI startup, no forced thinking phase — noticeably faster, roughly
    1-3s instead of 4-15s). This is the only backend that makes sense for
    a shared/hosted deployment — see "Hosting" below.
- **Live aggregate stats, not hardcoded text.** `rag.py`'s stats briefing
  used to be a literal string I hand-copied from one `analyse.py` run —
  a real bug: it would have silently gone stale the moment the underlying
  data changed (a re-tag, a new scrape, a taxonomy revision), with no way
  to tell from the outside. `get_stats_briefing()` now calls
  `fh_feedback.analyse.run()` live (cached 5 min, since it's a <1s query
  but no need to re-run it every message). The narrative findings (March
  2026 regression, the chronic address bug, the Restaurant Trust
  signal-vs-noise call) stay as static text — those are conclusions from
  a one-time investigation, not counts, so they don't go stale the same
  way and aren't worth re-deriving on every request.

## Hosting a shared deployment

Local single-user use needs none of this — just `python -m
fh_feedback.app.server`. A shared app other people can reach needs three
things the local version doesn't:

1. **`ANTHROPIC_API_KEY`** — get one at console.anthropic.com. Required:
   the `cli` backend uses your *personal* Claude Code login, which doesn't
   work for a multi-user service and means real usage-based billing per
   question, across everyone who uses it, not just you.
2. **`FEEDBACK_RADAR_TOKEN`** — a password of your choosing. Without it,
   anyone who finds the URL can ask unlimited questions on your API bill.
   The frontend prompts once and stores it in that browser's localStorage.
3. **`FEEDBACK_RADAR_RATE_LIMIT`** (optional, default 30/min) — a blunt
   global cap across all users, as a safety net against a runaway cost
   spike, not a per-person quota.

Deploy with the included `Dockerfile` + `fly.toml` (Fly.io — picked as a
sensible default for a small persistent Python service; Render or Railway
work too with the same Dockerfile, minimal changes):

```bash
fly launch --no-deploy         # first time only, accept/adjust the generated config
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
fly secrets set FEEDBACK_RADAR_TOKEN=<choose a password>
fly deploy
```

**The dataset is baked into the image**, not mounted live — `Dockerfile`
copies `data/processed/feedback.duckdb` in at build time. Updating the
hosted data (new reviews, re-tagging, a taxonomy change) means rebuilding
and redeploying the image (`fly deploy` again), not editing anything on
the running server.

**Not yet done, worth knowing:** no HTTPS-only cookie/session handling
beyond the bearer-token check (fine for an internal team tool, not for a
public-facing product); no per-user usage tracking, just the global rate
limit; the embedding model is loaded once per container instance, so a
cold-started machine (Fly's `auto_stop_machines`) pays that ~3.5s load on
its first request after waking up.

## Conventions
- `review_id` = `"<source>:<native_id>"`, stable across re-runs (used for dedupe).
- Never store display names; author identity is a salted hash only.
- Redact phone numbers / emails / order IDs from `text` before it reaches any
  LLM call or the embedding model.
- The tagging taxonomy lives in `.claude/skills/feedback-tagging/taxonomy.md`
  and is frozen once Jay approves it — changing it invalidates prior tags
  (re-run stage 4+ and bump `skill_version`).
- Scrapers: polite delay 3–6s between requests, honest UA (see `config.yaml`),
  collect by exact app ID / domain only — never by name search (other
  companies use "Foodhub"/"FoodHub" too).

## Out of scope for v1
Scheduling/automation, non-UK markets, internal data (support tickets, NPS),
a dashboard, API-based Claude calls, X/Facebook/TikTok.

**Trustpilot and Reddit were dropped after smoke-testing** (see
`src/fh_feedback/scrape/trustpilot.py` and `reddit.py` — code kept for later,
not run by default):
- Trustpilot sits behind an active AWS WAF JS bot-challenge; plain requests
  get a "Verifying your connection..." page, not content. Revisit via the
  official Trustpilot Business API or a paid vendor export (AppFollow/Appbot).
  This also means v1 has **no partner-voice source** (foodhub.com "Foodhub
  For Business" was Trustpilot too) — the report's partner appendix is empty
  for v1.
- Reddit's public `.json` endpoints now 403 unconditionally; needs a
  registered OAuth app at reddit.com/prefs/apps, which wasn't completed.

v1 sources are **App Store (gb) + Google Play (gb) only** — both diner-only,
consumer app reviews.
