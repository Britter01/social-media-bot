# Brite Tech Lifestyle — Social Media Automation

Automated content pipeline for **Brite Tech Lifestyle** (founder: Dean Britter — _"Technology, beautifully lived."_).

It researches trending topics with the Claude API's web search tool, generates captions and hashtags with the Claude API, thumbnails with Google Imagen 4 Fast, short videos with HeyGen (cloned voice), picks an optimal posting time per platform, and publishes to Instagram, X/Twitter, LinkedIn, YouTube, and TikTok — all on a schedule.

---

## How it works

```
scheduler/cron.py  (APScheduler worker on Railway)
│
├─ 05:30 daily ──▶ ResearchAgent
│                  Haiku 4.5 gathers trending topics via web search
│                  Sonnet 4.6 scores + assigns pillar / platform / angle
│                  → persisted to `topics` table as 'selected'
│                       │
│                       ▼  ── Human approval gate ──────────────────────┐
│               review_topics script (approve / reject)                 │
│               (set REQUIRE_TOPIC_APPROVAL=false to skip)              │
│                       │                                               │
│                       ▼  every 15 min                                 │
│               run_approved_pipeline                                   │
│               Sonnet 4.6 generates caption + hashtags (ContentAgent) │
│               Haiku 4.5 fixes repeated phrases (QualityAgent)        │
│               Sonnet 4.6 plans carousel copy (CarouselAgent)         │
│               Pillow renders 4 dark-card slides + scene cover        │
│               slides uploaded to Supabase Storage                    │
│               SchedulerAgent picks optimal slot (no LLM)             │
│               → `posts` row: status=scheduled                        │
│               + auto cross-posts IG/LI → Facebook (same caption)     │
│                                                                       │
│                                                              ─────────┘
├─ 06:00 daily ──▶ run_content_pipeline  (fallback / extra posts)
│                  Same ContentAgent / CarouselAgent / SchedulerAgent flow
│
├─ 02:00 nightly ▶ run_image_refresh
│                  Finds any scheduled/failed IG or FB post with no slides
│                  Re-runs CarouselAgent to regenerate them
│
├─ every 5 min ──▶ run_publisher
│                  Claims posts whose scheduled_time has passed
│                  PublisherAgent posts to IG / FB / X / LI / YT / TT
│                  IG & FB → 4-slide carousel (Graph API)
│                  Others → single image or video
│
└─ every 2 hr ───▶ run_analytics
                   AnalyticsAgent fetches engagement at 24h + 7d after publish
                   Stores reach / impressions / likes / comments → `post_analytics`
```

The **research agent** runs first: it uses Claude's server-side web search tool to find trending topics across the brand's themes, scores each for brand fit with structured outputs, and stores them in the `topics` table.

**Human approval gate (on by default).** Researched topics are stored as `selected` (awaiting review), not posted. You review them and approve or reject each:

```bash
python -m scripts.review_topics                 # list topics awaiting review
python -m scripts.review_topics --approve 1a2b   # approve by id (prefix is fine)
python -m scripts.review_topics --reject 9f3c
python -m scripts.review_topics --interactive    # review one at a time
```

Approved topics are picked up by the worker (every 15 min) and turned into scheduled posts. Set `REQUIRE_TOPIC_APPROVAL=false` to skip the gate and post automatically.

A topic moves `new → selected → approved → used` (or `rejected`); from approval, each post moves through `draft → content_ready → media_ready → scheduled → publishing → published` (or `failed`). The publisher loop picks up any post whose `scheduled_time` has passed. (The 06:00 content pipeline also runs independently as a fallback source of posts.)

### Content pillars
AI Guide · Tech Lifestyle · Productivity · Fitness Tech · Review

### Brand voice
Clear, confident, warm. Never patronising. Short sentences. (Baked into the cached Claude system prompt in `agents/content_agent.py`.)

### Model usage (cost-tiered, no Opus)
Each agent uses the cheapest model that does its job well — Opus isn't used anywhere.

| Task | Model | Agent | Why |
| --- | --- | --- | --- |
| Caption + hashtags | `ANTHROPIC_MODEL_CREATIVE` — Sonnet 4.6 | `content_agent` | Creative writing drives engagement; hashtags ride along in the same call. |
| Carousel copy (slide headlines, body, CTA) | Sonnet 4.6 | `carousel_agent` | Brand-voice judgment needed for punchy slide copy; no image model required — slides are pure Pillow. |
| Topic scoring / pillar + platform + angle | Sonnet 4.6 | `research_agent` | Ideation/judgment step; small structured output. |
| Trend discovery / web search | `ANTHROPIC_MODEL_FAST` — Haiku 4.5 | `research_agent` | High-token gather-and-summarise; the search is server-side so a cheap model suffices. |
| Text QC / repeated-phrase fix | Haiku 4.5 | `quality_agent` | Low-stakes mechanical fix; fast + cheap. |
| Scheduling | none | `scheduler_agent` | Deterministic best-time table lookup — zero tokens. |
| Thumbnails (single-image posts) | none — Imagen 4 Fast | `thumbnail_agent` | Image generation, not text. |
| Video (YouTube/TikTok) | none — HeyGen | `video_agent` | Cloned-voice video generation. |
| Publishing / analytics | none — platform APIs | `publisher_agent`, `analytics_agent` | REST API calls only. |

---

## Project layout

```
core/
  config.py        Loads + validates all env vars; the Config singleton.
  models.py        Post / Topic / Brand data models, Pillar/Platform/Status enums.
  database.py      Supabase CRUD for the `posts`, `topics`, and `post_analytics` tables.
  storage.py       Supabase Storage uploader (public URLs for media).
  cover_image.py   Lifestyle scene overlay — warps a text card onto a scene photo
                   for carousel cover slides (perspective transform via Pillow).
agents/
  research_agent.py   Trending-topic discovery + scoring (Claude web search
                      tool, structured outputs); seeds the content agent.
                      Auto cross-posts every IG/LI topic to Facebook.
  content_agent.py    Captions + hashtags (Claude, adaptive thinking,
                      prompt caching, structured outputs).
  carousel_agent.py   4-slide text carousels for Instagram + Facebook (Claude
                      plans copy, Pillow renders dark brand cards). No image
                      model — slides are 100% deterministic.
  thumbnail_agent.py  Single images via Imagen 4 Fast for non-carousel platforms.
  video_agent.py      Short videos via HeyGen with a cloned voice.
  quality_agent.py    Text QC (repeated-phrase fix) and image sanity check.
  publisher_agent.py  Posts to Instagram / Facebook / X / LinkedIn / YouTube / TikTok.
  scheduler_agent.py  Optimal posting time per platform.
  analytics_agent.py  Fetches engagement metrics (reach, impressions, likes…)
                      from each platform API at 24h and 7d after publish.
scheduler/
  cron.py          APScheduler worker: all pipeline jobs + publisher loop.
scripts/
  smoke_test.py    Run one post end-to-end in dry-run mode.
  review_topics.py Approve/reject researched topics (the human gate).
tests/             Hermetic pytest suite (external SDKs faked).
```

---

## Setup

### 1. Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in `.env`. **Leave `DRY_RUN=true` until you've confirmed everything works** — in dry-run nothing is posted to any real platform.

| Variable group | Keys | Where to get them |
| --- | --- | --- |
| Claude | `ANTHROPIC_API_KEY` (model tiers default to Sonnet 4.6 / Haiku 4.5; override with `ANTHROPIC_MODEL_CREATIVE` / `ANTHROPIC_MODEL_FAST`) | platform.claude.com |
| Imagen | `GOOGLE_API_KEY` | Google AI Studio / Vertex |
| HeyGen | `HEYGEN_API_KEY`, `HEYGEN_VOICE_ID`, `HEYGEN_AVATAR_ID` | HeyGen dashboard |
| Supabase | `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_BUCKET` | Supabase project settings |
| Instagram | `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_BUSINESS_ACCOUNT_ID` | Meta Graph API |
| X/Twitter | `TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_SECRET` | X developer portal |
| LinkedIn | `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AUTHOR_URN` | LinkedIn developer app |
| YouTube | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | Google Cloud console |
| TikTok | `TIKTOK_ACCESS_TOKEN` | TikTok developer portal |

Credentials are optional per platform — the pipeline only targets platforms whose keys are present (`Config.configured_platforms()`).

### 3. Create the Supabase table + bucket

In the Supabase SQL editor, run the DDL from the docstring at the top of `core/database.py`, then create a public storage bucket:

```sql
insert into storage.buckets (id, name, public)
values ('media', 'media', true)
on conflict (id) do nothing;
```

---

## Task shortcuts

A `Makefile` (Unix/CI) and `tasks.ps1` (Windows) wrap the common commands:

| Make | PowerShell | Does |
| --- | --- | --- |
| `make install-dev` | `./tasks.ps1 install-dev` | Install runtime + dev deps |
| `make test` | `./tasks.ps1 test` | Run the test suite |
| `make smoke` | `./tasks.ps1 smoke` | Dry-run one post end-to-end |
| `make run` | `./tasks.ps1 run` | Start the scheduler worker |
| `make lint` | `./tasks.ps1 lint` | Lint with ruff |
| `make format` | `./tasks.ps1 format` | Auto-format + fix with ruff |
| `make clean` | `./tasks.ps1 clean` | Remove caches |

Run `make` (or `./tasks.ps1 help`) with no argument to list them.

## Run

### Smoke test (no infrastructure needed)

```bash
python -m scripts.smoke_test
python -m scripts.smoke_test --pillar "Review" --platform linkedin --topic "noise-cancelling earbuds"
```

Runs one post through all four stages with publishing forced to dry-run. Stages whose API key is missing are skipped with a clear message, so it works even with an empty `.env`.

### The worker

```bash
python scheduler/cron.py
```

This is the long-running process. It researches trending topics daily at 05:30 (stored for review), turns approved topics into scheduled posts every 15 minutes, generates and schedules a fallback batch at 06:00 (brand timezone), and publishes due posts every 5 minutes. Adjust the cadence in `scheduler/cron.py` (`build_scheduler`). Review researched topics with `python -m scripts.review_topics`.

---

## Tests

The suite is hermetic — it fakes the external SDKs, so it needs no API keys and makes no network calls.

```bash
pip install -r requirements-dev.txt   # or just: pip install pytest
pytest
```

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request. It installs
`requirements-dev.txt`, byte-compiles every module, runs `pytest` on Python
3.11 and 3.12, and executes the dry-run smoke test (no credentials needed).

---

## Deploy (Heroku-style worker)

The `Procfile` defines a single worker dyno:

```
worker: python scheduler/cron.py
```

```bash
heroku create
heroku config:set ANTHROPIC_API_KEY=... GOOGLE_API_KEY=... SUPABASE_URL=... # etc.
git push heroku main
heroku ps:scale worker=1
```

The same `Procfile`/env-var model works on Railway, Render, Fly.io, or any container platform — set the environment variables and run the `worker` command.

---

## Operational notes

- **Go live carefully.** Keep `DRY_RUN=true` for the first deploy, watch the logs, then set it to `false`.
- **Media URLs.** Instagram and TikTok need publicly reachable media. With Supabase Storage configured, thumbnails are uploaded automatically and a public URL is stored on the post. HeyGen returns hosted video URLs directly.
- **Failure isolation.** One post or one platform failing never crashes the worker — failures are logged and the post is marked `failed`.
- **Publish-once.** Before publishing, the worker atomically claims a post by conditionally flipping its row `scheduled → publishing` (only one worker can win), and the publisher itself is idempotent (a post that already has a platform id is skipped). Safe to run multiple worker instances.
- **Tuning post times.** The optimal-slot tables live in `agents/scheduler_agent.py`. Replace the defaults with your own engagement analytics over time.
- **Cost.** No Opus anywhere (see the Model usage table). The content agent's brand brief is sized to clear Sonnet's ~2048-token prompt-cache minimum, so within a batch of posts the first generation writes the cached prefix (~1.25x) and the rest read it (~0.1x) instead of re-paying full price. The research calls run once a day on different models, so they aren't cached (a cache write would never be read back within the 5-minute TTL).
