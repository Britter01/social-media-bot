# Brite Tech Lifestyle — Social Media Automation

Automated content pipeline for **Brite Tech Lifestyle** (founder: Dean Britter — _"Technology, beautifully lived."_).

It researches trending topics with the Claude API's web search tool, generates captions and hashtags with the Claude API, thumbnails with Google Imagen 4 Fast, short videos with HeyGen (cloned voice), picks an optimal posting time per platform, and publishes to Instagram, X/Twitter, LinkedIn, YouTube, and TikTok — all on a schedule.

---

## How it works

```
                        scheduler/cron.py  (APScheduler worker)
                                 │
   daily 05:30 ──▶ ResearchAgent (Claude + web search)
                     │  finds + scores trending topics → `topics` row
                     ▼  hands the best to ↓
   daily 06:00 ─────────────────┤                          every 5 min
                                ▼                                 │
   ContentAgent  ──▶  Thumbnail/Video agents  ──▶  SchedulerAgent ▼
   (Claude API)       (Imagen 4 Fast / HeyGen)     (optimal slot)  PublisherAgent
        │                      │                        │          (IG/X/LI/YT/TT)
        └──────────────── persisted to Supabase as a `posts` row ──┘
```

The **research agent** runs first: it uses Claude's server-side web search tool to find trending topics across the brand's themes, scores each for brand fit with structured outputs, stores them in the `topics` table, and turns the highest-scoring ones into draft posts. From there each post moves through statuses: `draft → content_ready → media_ready → scheduled → publishing → published` (or `failed`). The publisher loop picks up any post whose `scheduled_time` has passed. (The 06:00 content pipeline also runs independently as a fallback source of posts.)

### Content pillars
AI Guide · Tech Lifestyle · Productivity · Fitness Tech · Review

### Brand voice
Clear, confident, warm. Never patronising. Short sentences. (Baked into the cached Claude system prompt in `agents/content_agent.py`.)

---

## Project layout

```
core/
  config.py        Loads + validates all env vars; the Config singleton.
  models.py        Post / Topic / Brand data models, Pillar/Platform/Status enums.
  database.py      Supabase CRUD for the `posts` and `topics` tables.
  storage.py       Supabase Storage uploader (public URLs for media).
agents/
  research_agent.py   Trending-topic discovery + scoring (Claude web search
                      tool, structured outputs); seeds the content agent.
  content_agent.py    Captions + hashtags (Claude, adaptive thinking,
                      prompt caching, structured outputs).
  thumbnail_agent.py  Images via Imagen 4 Fast (imagen-4.0-fast-generate-001).
  video_agent.py      Short videos via HeyGen with a cloned voice.
  publisher_agent.py  Posts to Instagram / X / LinkedIn / YouTube / TikTok.
  scheduler_agent.py  Optimal posting time per platform.
scheduler/
  cron.py          APScheduler worker: content pipeline + publisher loop.
scripts/
  smoke_test.py    Run one post end-to-end in dry-run mode.
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
| Claude | `ANTHROPIC_API_KEY` | platform.claude.com |
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

This is the long-running process. It researches trending topics and seeds content daily at 05:30, generates and schedules a fallback batch at 06:00 (brand timezone), and publishes due posts every 5 minutes. Adjust the cadence in `scheduler/cron.py` (`build_scheduler`).

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
- **Cost.** The content agent caches the large brand system prompt, so repeated generations in a run pay full price only for the first call.
