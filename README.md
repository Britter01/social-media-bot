# Brite Tech Lifestyle — Social Media Automation

Automated content pipeline for **Brite Tech Lifestyle** (founder: Dean Britter — _"Technology, beautifully lived."_).

Researches trending topics with the Claude API's web search tool, generates captions and hashtags, renders infographic reels and carousels with Pillow, single-image thumbnails with Google Imagen 4 Fast, and short videos with HeyGen (cloned voice). Picks an optimal posting time per platform, adds ±15 min jitter, and publishes to Instagram (via Telegram for native reach), X/Twitter, LinkedIn, Facebook, YouTube, and TikTok — all on a schedule.

---

## How it works

```
scheduler/cron.py  (APScheduler worker on Railway)
│
├─ 05:30 daily ──▶ ResearchAgent
│                  Haiku 4.5 gathers trending topics via Claude web search
│                  Sonnet 4.6 scores + assigns pillar / platform / angle
│                  → persisted to `topics` table as 'pending_approval'
│                       │
│                       ▼  ── Human approval gate ──────────────────────────┐
│               Dashboard → Topics tab (approve / reject)                   │
│               (set REQUIRE_TOPIC_APPROVAL=false to skip)                  │
│                       │                                                   │
│                       ▼  every 15 min                                     │
│               run_approved_pipeline                                       │
│               Sonnet 4.6 generates caption + hashtags (ContentAgent)     │
│               Haiku 4.5 fixes repeated phrases (QualityAgent)            │
│               Sonnet 4.6 plans carousel/infographic copy                 │
│               Pillow renders 5 stat-card slides (InfographicAgent) OR    │
│               4 text-card slides (CarouselAgent) + scene cover           │
│               (all rendered text is emoji-sanitised — no tofu squares)   │
│               Slides uploaded to Supabase Storage                        │
│               SchedulerAgent picks optimal slot ± 15 min jitter          │
│               → `posts` row: status=scheduled                            │
│               + auto cross-posts IG/LI → Facebook carousel               │
│                                                                           │
│                                                                ───────────┘
├─ 06:00 daily ──▶ run_content_pipeline  (fallback / extra posts)
│
├─ 11:00 daily ──▶ run_infographic_pipeline  (scheduled infographic reel)
│
├─ 12:00 daily ──▶ run_daily_ai_news
│                  Auto-generates an AI & tech news carousel from the day's
│                  top stories — no topic approval required. Target platform
│                  (Instagram / Facebook / both) follows the saved default
│                  set from the dashboard News Platforms selector.
│
├─ Monday 07:00 ─▶ run_weekly_strategy
│                  Studies competitor accounts & viral patterns
│                  Generates 7 shaped content ideas for approval
│
├─ 02:00 nightly ▶ run_image_refresh
│                  Finds scheduled/failed posts with no slides
│                  Re-runs the appropriate media agent to regenerate them
│
├─ every 5 min ──▶ run_publisher
│                  Claims posts whose scheduled_time has passed
│                  Instagram  → sends image to Telegram for native posting
│                               (API mode available via dashboard toggle)
│                  Facebook   → 4-slide carousel via Graph API
│                  X/Twitter  → image + caption via Twitter API v2
│                  LinkedIn   → ugcPost with image asset
│                  YouTube    → upload via YouTube Data API
│                  TikTok     → upload via TikTok Content Posting API
│                  (Facebook / X / LinkedIn can each be switched to
│                   Telegram delivery per-platform in the dashboard)
│
├─ every 2 hrs ──▶ run_analytics
│                  Fetches engagement at 24 h + 7 d after publish
│                  Stores reach / impressions / likes / comments
│
├─ every 4 hrs ──▶ run_qc_retry           (regenerate failed thumbnails)
├─ every 15 sec ─▶ run_pending_commands   (dashboard command queue — the
│                  control plane behind every button; also reaps commands
│                  stuck 'running' after a worker restart)
├─ Sunday 03:00 ▶ run_cleanup_commands    (prune old command rows; keeps
│                  pause/mode state rows)
└─ Sunday 04:00 ▶ run_token_refresh       (refresh the Meta long-lived token)
```

### Instagram publishing strategy

Instagram posts are routed to **Telegram** by default rather than published via the Graph API. Posts published through the API consistently receive suppressed reach (1–4 views) compared to native app uploads (60–70+ views). The bot sends the ready image and caption to your Telegram; you save the image and post it in the Instagram app for full organic reach.

When you can't check Telegram, switch to **API mode** via the Platform Modes panel in the dashboard sidebar — the bot publishes directly and switches back automatically once you toggle it off.

### Per-platform Telegram delivery

Facebook, X/Twitter, and LinkedIn publish directly via their APIs by default, but each can be switched to **Telegram delivery** independently from the **Platform Modes** panel — the same manual-native-posting flow Instagram uses. Useful when you'd rather post that platform by hand for reach.

### Per-platform pipeline control

Every platform has **two independent switches** in the sidebar **Platform Publishing** panel:

- **Content** — pauses topic research *and* content generation for that platform. No new topics are assigned to it, and its already-approved topics stop becoming posts (they are held, not rejected, so they resume automatically when you switch it back on).
- **Publishing** — holds that platform's scheduled posts in the queue (a manual **Publish Now** on an individual post still works).

Platforms with no API credentials on the worker show as **"not set up"** rather than offering pause/resume buttons, so the dashboard never implies a platform is posting when it can't. A master **Content Generation** switch (above the per-platform ones) pauses all research and generation across every platform at once.

### Human approval gate

Researched topics land in the dashboard **Topics** tab as `pending_approval`. You review and approve or reject each one; nothing is posted without your sign-off.

```bash
# Or use the CLI:
python -m scripts.review_topics                  # list pending topics
python -m scripts.review_topics --approve 1a2b   # approve by id prefix
python -m scripts.review_topics --reject 9f3c
python -m scripts.review_topics --interactive    # step through one at a time
```

Set `REQUIRE_TOPIC_APPROVAL=false` to skip the gate and post automatically.

### Content pillars

AI Guide · Tech Lifestyle · Productivity · Fitness Tech · Review

### Brand voice

Clear, confident, warm. Never patronising. Short sentences. (Baked into the cached Claude system prompt in `agents/content_agent.py`.)

### Model usage (cost-tiered)

| Task | Model | Agent |
|------|-------|-------|
| Caption + hashtags | Sonnet 4.6 | `content_agent` |
| Infographic copy (5 stat cards) | Sonnet 4.6 | `infographic_agent` |
| Carousel slide copy | Sonnet 4.6 | `carousel_agent` |
| AI news carousel | Sonnet 4.6 | `infographic_agent` |
| Topic scoring / pillar + platform | Sonnet 4.6 | `research_agent` |
| Trend discovery / web search | Haiku 4.5 | `research_agent` |
| Text QC / repeated-phrase fix | Haiku 4.5 | `quality_agent` |
| Scheduling | none — deterministic | `scheduler_agent` |
| Thumbnails | none — Imagen 4 Fast | `thumbnail_agent` |
| Video | none — HeyGen | `video_agent` |
| Publishing / analytics | none — platform APIs | `publisher_agent`, `analytics_agent` |

---

## Project layout

```
core/
  config.py          Loads + validates all env vars; the Config singleton.
  models.py          Post / Topic / Brand data models, enums.
  database.py        Supabase CRUD for posts, topics, post_analytics tables.
  storage.py         Supabase Storage uploader (public URLs for media).
  cover_image.py     Perspective-warps a text card onto a lifestyle scene photo.
  telegram_notify.py Sends posts to Telegram for native posting (any platform).
  image_utils.py     Shared Pillow helpers (text wrapping, font loading,
                     emoji/smart-punctuation sanitising for rendered text).
agents/
  research_agent.py    Trending-topic discovery + scoring (Claude web search).
  content_agent.py     Captions + hashtags (Claude, prompt caching).
  carousel_agent.py    4-slide text carousels for Facebook (Claude + Pillow).
  infographic_agent.py 5-stat-card infographic reels for Instagram (Claude + Pillow).
  thumbnail_agent.py   Single images via Imagen 4 Fast.
  video_agent.py       Short videos via HeyGen with a cloned voice.
  quality_agent.py     Text QC and image sanity check.
  publisher_agent.py   Routes posts to Telegram (Instagram) or platform APIs.
  scheduler_agent.py   Optimal posting time with ±15 min jitter.
  analytics_agent.py   Engagement metrics at 24 h and 7 d after publish.
scheduler/
  cron.py            APScheduler worker: all pipeline jobs + publisher loop.
dashboard/
  app.py             Streamlit dashboard (topics, posts, analytics, pipeline controls).
scripts/
  smoke_test.py      Run one post end-to-end in dry-run mode.
  review_topics.py   Approve/reject researched topics (the human gate).
tests/               Hermetic pytest suite (external SDKs faked, no network).
```

---

## Setup

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate       # macOS / Linux
# .venv\Scripts\activate        # Windows

pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in `.env`. **Leave `DRY_RUN=true` until you've confirmed everything works** — in dry-run mode nothing is posted to any real platform.

| Variable group | Keys | Where to get them |
|----------------|------|-------------------|
| Claude | `ANTHROPIC_API_KEY` | platform.anthropic.com |
| Imagen | `GOOGLE_API_KEY` | Google AI Studio |
| HeyGen | `HEYGEN_API_KEY`, `HEYGEN_VOICE_ID`, `HEYGEN_AVATAR_ID` | HeyGen dashboard |
| Supabase | `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_BUCKET` | Supabase project settings |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | BotFather → your bot; @userinfobot for your chat ID |
| Instagram | `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_BUSINESS_ACCOUNT_ID` | Meta Graph API |
| Facebook | `FACEBOOK_PAGE_ID` | Meta developer portal |
| X/Twitter | `TWITTER_API_KEY`, `TWITTER_API_SECRET`, `TWITTER_ACCESS_TOKEN`, `TWITTER_ACCESS_SECRET` | X developer portal |
| LinkedIn | `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AUTHOR_URN`, `LINKEDIN_ORG_URN`, `LINKEDIN_POST_TARGETS` | LinkedIn developer app |
| YouTube | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | Google Cloud console |
| TikTok | `TIKTOK_ACCESS_TOKEN` | TikTok developer portal |
| Dashboard | `DASHBOARD_PASSWORD` | Set any strong password |

Credentials are optional per platform — the pipeline only targets platforms whose keys are present (`Config.configured_platforms()`).

### 3. Create the Supabase tables + bucket

In the Supabase SQL editor, run the DDL from the docstring at the top of `core/database.py`, then create a public storage bucket:

```sql
insert into storage.buckets (id, name, public)
values ('media', 'media', true)
on conflict (id) do nothing;
```

### 4. Set up Telegram (Instagram delivery)

1. Open Telegram and message **@BotFather** → `/newbot` → follow prompts → copy the bot token to `TELEGRAM_BOT_TOKEN`.
2. Message your new bot once (it won't reply, that's fine).
3. Message **@userinfobot** → `/start` → it will reply with your user ID → copy it to `TELEGRAM_CHAT_ID`.

The bot will now send your Instagram posts to you on Telegram whenever the publisher runs.

---

## Task shortcuts

| Make | PowerShell | Does |
|------|------------|------|
| `make install-dev` | `./tasks.ps1 install-dev` | Install runtime + dev deps |
| `make test` | `./tasks.ps1 test` | Run the test suite |
| `make smoke` | `./tasks.ps1 smoke` | Dry-run one post end-to-end |
| `make run` | `./tasks.ps1 run` | Start the scheduler worker |
| `make lint` | `./tasks.ps1 lint` | Lint with ruff |
| `make format` | `./tasks.ps1 format` | Auto-format + fix with ruff |
| `make clean` | `./tasks.ps1 clean` | Remove caches |

---

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

Long-running process. Researches topics daily at 05:30, turns approved topics into scheduled posts every 15 minutes, generates a content fallback batch at 06:00, generates an AI news carousel at 11:00, runs the weekly competitor strategy on Mondays at 07:00, and publishes due posts every 5 minutes.

---

## Dashboard

```bash
streamlit run dashboard/app.py
```

| Tab | What it shows |
|-----|---------------|
| **Topics** | Researched topics awaiting your approval |
| **In Progress** | Posts whose copy is written but media isn't generated yet. **Generate & schedule** (per-post or all at once) finishes the media and moves them to Scheduled |
| **Scheduled** | Posts with a future publish time; edit captions, **Publish Now** (that single post only), or dismiss |
| **Generated** | Posts in `manual_ready` — posts delivered to Telegram, plus any held posts. Post Now / Schedule / Mark as Posted / Dismiss per card |
| **Calendar** | Monthly view of scheduled posts (navigable) |
| **Published** | All published posts with platform links |
| **Analytics** | Engagement metrics (reach, impressions, likes) |
| **Pipeline** | Process diagram + **Storage archive & cleanup** (download published-post media to a ZIP and free Supabase Storage) |

The sidebar contains all controls: pause/resume automation, master content-generation switch, per-platform Content + Publishing switches, the AI News platform selector, pipeline runs, Platform Modes (Instagram API mode + per-platform Telegram delivery), and maintenance tools (System Check, token refresh, reset stuck posts, hashtag trim). Pipeline controls run as a Streamlit *fragment*, so clicking a button updates just that panel instead of reloading the whole page. On mobile, the same controls appear in the main body.

### How the dashboard talks to the worker

The dashboard never runs pipeline work itself — it inserts rows into the `pipeline_commands` table, and the worker's command queue (polled every 15 s) executes them and writes back the result. A **worker health banner** appears at the top if commands sit unprocessed, which means the Railway worker is down or deploying. Because of the queue, buttons take a few seconds (media/publish actions a few minutes) to reflect — that's the round-trip, not a hang.

---

## Tests

The suite is hermetic — fakes all external SDKs, needs no API keys, makes no network calls.

```bash
pip install -r requirements-dev.txt
pytest
```

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request: installs deps, byte-compiles every module, runs pytest on Python 3.11 and 3.12, and executes the dry-run smoke test.

---

## Deploy

The `Procfile` defines a single worker dyno (tested on Railway):

```
worker: python scheduler/cron.py
```

Set all environment variables in your platform's dashboard and run the `worker` command. Railway, Render, Fly.io, and Heroku all work with this pattern.

---

## Operational notes

- **Go live carefully.** Keep `DRY_RUN=true` for the first deploy. Watch the logs. Then set it to `false`.
- **Instagram reach.** Use Telegram delivery (default) for organic reach. Switch to API mode only when you can't check Telegram — then switch back.
- **Two levels of pause.** The **Pause Automation** button stops every scheduled job instantly. Below it, the master **Content Generation** switch pauses only research + generation, and each platform has its own **Content** and **Publishing** switches. Resume any of them when ready.
- **Publish Now targets one post.** A per-post Publish Now publishes only that post (bypassing its own platform pause) — it never sweeps up other platforms' overdue posts. The bulk **Publish Due Posts** button honours platform pauses.
- **Media URLs.** Instagram and TikTok need publicly reachable media. Supabase Storage handles this automatically.
- **Storage housekeeping.** Supabase Storage fills up over time. The **Storage archive & cleanup** section (Pipeline tab) bundles media for published/dismissed posts into a ZIP for local backup, then deletes it from Supabase. It never touches cached backgrounds/templates, config flags, or anything under 7 days old.
- **Emoji-free rendered text.** All text drawn into images is sanitised (emoji stripped, smart punctuation mapped to ASCII) so brand fonts never render a "tofu" square. Captions keep the same no-emoji rule.
- **Failure isolation.** One failing post or platform never crashes the worker. Failures are logged and the post is marked `failed`. A failed Telegram delivery marks the post `failed` (not silently "sent") so you can retry it.
- **Publish-once.** The worker atomically claims each post before publishing (`scheduled → publishing`), and the publisher skips posts that already have a platform ID. Commands are claimed atomically too (pending → running), so overlapping workers during a deploy can't run one twice. Safe to run multiple worker instances.
- **Tuning post times.** Optimal-slot tables live in `agents/scheduler_agent.py`. Replace defaults with your own engagement data over time.
- **LinkedIn goes to the profile *and* the page.** Each LinkedIn post targets both the personal profile and the Brite Tech Lifestyle company page (`LINKEDIN_POST_TARGETS`: `both` | `personal` | `organization`).
  - *Personal profile* — published automatically via the API, as always.
  - *Company page* — API publishing needs the `w_organization_social` scope, which LinkedIn grants only to apps approved for its **Community Management API**. Without that approval the page copy is **sent to Telegram** for manual posting instead (`LINKEDIN_PAGE_TELEGRAM_FALLBACK=true`, the default) — the same manual route Instagram uses. The personal post is never affected by a page failure.
  - *If you do get API access*: set `LINKEDIN_ORG_URN` to the page's `urn:li:organization:NNNNN` (or leave it blank to auto-discover pages the token administers). The page share is also the only LinkedIn post with an analytics endpoint, so it becomes the post's primary `platform_post_id` and unlocks engagement metrics — personal-profile posts have no analytics API at all.
- **Token refresh.** LinkedIn and Meta tokens expire. Use the Refresh Meta Token button in the dashboard Maintenance panel, or re-authorise the LinkedIn app and update `LINKEDIN_ACCESS_TOKEN`.
- **Supabase key.** Use the **service role** key for `SUPABASE_KEY` (not the anon key) so the worker and dashboard bypass Row Level Security and can read/write all rows and list Storage objects (the archive scan needs the latter).
- **Google Drive is not used.** The pipeline uses Google only for **Imagen** (`GOOGLE_API_KEY`, thumbnails) and the **YouTube Data API** (publishing). There is no Google Drive integration anywhere in the app.
