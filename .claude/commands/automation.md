# Brite Tech Lifestyle — Automation System Knowledge Base

TRIGGER — load this skill BEFORE writing, editing, or debugging any code in this repository. Do not skip it because a task looks small or obvious — the lessons here prevent re-learning the same mistakes. Auto-invoke whenever:
- any file in this repo is being edited (agents/, core/, scheduler/, dashboard/, tests/)
- a new feature, pipeline step, or platform is being added
- a bug or unexpected behaviour is being investigated
- architectural or integration decisions are being made (publishing strategy, IPC, feature flags, Streamlit patterns)
- the user asks about how something works or why it was built a certain way

This file captures every decision, what worked, what failed, and how to reproduce the system.

---

## What this system is

A fully automated social media content pipeline for **Brite Tech Lifestyle** (Dean Britter). It researches trending topics, generates captions/hashtags, renders infographic reels and carousels using Pillow (no image model), and publishes to Instagram (via Telegram for native reach), Facebook, X/Twitter, LinkedIn, YouTube, and TikTok on a schedule. All infrastructure runs on Railway. The dashboard is a Streamlit app.

**Stack:** Python 3.11+, APScheduler, Supabase (Postgres + Storage), Streamlit, Anthropic API (Sonnet 4.6 / Haiku 4.5), Google Imagen 4 Fast, HeyGen, httpx, Pillow.

---

## Architecture overview

```
scheduler/cron.py          Long-running APScheduler worker (Railway)
  └─ jobs poll DB for pipeline_commands table (user → cron IPC)

core/
  config.py                Single Config dataclass, all env vars loaded once
  models.py                Post / Topic dataclasses + PostStatus / Platform enums
  database.py              Supabase CRUD — all queries go through here
  storage.py               Supabase Storage (public URLs for media)
  telegram_notify.py       Sends Instagram posts to Telegram
  image_utils.py           Shared Pillow helpers (font loading, text wrapping)
  cover_image.py           Perspective-warp lifestyle scenes for carousel covers

agents/
  publisher_agent.py       Routes posts: Instagram→Telegram, others→platform API
  scheduler_agent.py       Optimal slot lookup + ±15 min random jitter
  infographic_agent.py     5-stat-card reel generator (Claude + Pillow, ~3 000 lines)
  carousel_agent.py        4-slide text carousel (Claude + Pillow) — Facebook only
  content_agent.py         Captions + hashtags (Claude, prompt caching)
  research_agent.py        Web search topic discovery + scoring
  quality_agent.py         Text QC + image sanity check
  thumbnail_agent.py       Imagen 4 Fast single images
  video_agent.py           HeyGen cloned-voice videos
  analytics_agent.py       Engagement metrics at 24 h + 7 d

dashboard/app.py           Streamlit dashboard (~2 600 lines)
```

---

## Key architectural decisions and WHY

### 1. Instagram → Telegram routing (not Graph API)

**Decision:** All Instagram posts are routed to Telegram by default. The publisher sends the image + caption to a Telegram bot, and the user posts natively in the Instagram app.

**Why:** The Instagram Graph API consistently suppresses the reach of API-published posts to 1–4 views. Native app uploads receive 60–70+ views for the same content. This is a documented Meta behaviour for business accounts using the API.

**Implementation:**
- `publisher_agent.py`: checks `_is_instagram_api_mode()` first; if False (default), calls `_route_instagram_to_telegram()`
- `post.status` → `PostStatus.MANUAL_READY`
- `post.meta["delivery"] = "telegram"` — used by dashboard to show the right buttons
- `core/telegram_notify.py`: `send_instagram_post()` handles single images (sendPhoto), carousels (sendMediaGroup), reels (sendMessage with video URL)
- `PostStatus.MANUAL_READY` posts appear in the Generated tab with "Sent to Telegram", "Resend", and "Mark as Posted" buttons

**API mode toggle:**
- Supabase Storage flag: `config/instagram.api_mode` (file presence = API mode on; absence = Telegram mode)
- `_is_instagram_api_mode()` downloads this file; returns False on any error (fail-open = stay in Telegram mode)
- Dashboard sidebar has an Instagram panel with a toggle button
- Commands: `instagram_api_mode` / `instagram_telegram_mode` in the pipeline_commands table

### 2. Pipeline command IPC (dashboard → scheduler)

**Decision:** Dashboard writes command strings to a `pipeline_commands` Supabase table. The scheduler polls this table every 2 minutes and executes matching `run_*` functions.

**Why:** Railway doesn't allow direct process communication between the Streamlit web app and the scheduler worker dyno. A shared database row is the simplest reliable IPC.

**Cooldown pattern:** `_queue_command(cmd, cooldown_key=...)` uses `st.session_state` to prevent double-submitting within 10 seconds. The scheduler marks each command as executed in the DB after running.

**Command format:** Plain string for simple commands (`"research"`, `"publish"`). Pipe-delimited for parameterised commands (`"create_infographic|custom topic here"`). Max topic length capped at 200 chars before queuing (security: prompt injection prevention).

### 3. Supabase Storage as feature-flag store

**Pattern used for:** automation pause flag, Instagram API mode flag.

**How:** Upload a small text file to a known path to activate; delete it to deactivate.
- Pause flag: `config/automation_paused`
- Instagram API mode: `config/instagram.api_mode`
- `get_storage().download(path)` returns the content or raises — check with try/except, treat any exception as flag absent

**Why not a DB column:** Storage flags don't need migrations, are easy to inspect, and pattern-match the existing cron/pause logic already in place.

### 4. APScheduler ±15 min jitter

**Why:** Posts that always land at exactly 09:00 or 17:00 look robotic to platform algorithms. Adding randomness makes the posting pattern indistinguishable from a human.

**Implementation:** `scheduler_agent.py` calls `random.randint(-15, 15)` and adds `timedelta(minutes=...)` to the best slot found by the optimal-time lookup table.

### 5. Infographic reel generation (Pillow only, no image model)

**Decision:** All infographic slides are rendered entirely with Pillow — no Imagen, no Stable Diffusion, no external image model.

**Why:** Image models fail unpredictably. A pure Pillow pipeline always generates a result; there's no API to throttle, no generation to time out, no content filters to reject the prompt.

**Scale:** `infographic_agent.py` is ~3 000 lines. It has multiple visual themes (wheel, dark, light, rich), handles font loading with Google Fonts fallback, and renders stat cards, title cards, and cover images.

**Font support:** Brand fonts (BriteHero-Bold, Figtree-Regular, PlayfairDisplay-Italic) lack many Unicode glyphs. `_strip_emojis()` in the agent now also:
1. Replaces smart punctuation (curly quotes, em/en dashes, bullets, arrows, math symbols) with ASCII equivalents via `str.maketrans`
2. Strips any remaining characters above U+024F (Latin Extended) using a regex catch-all
This prevents tofu boxes (□) appearing in rendered text.

### 6. Streamlit tab persistence across reruns

**Problem:** Every Streamlit button click reruns the entire script, and React resets the active tab to index 0.

**What didn't work:** A `setInterval` that runs once on iframe load (25 retries × 120 ms). Streamlit caches the `components.html` iframe between reruns when the HTML string is unchanged, so the one-shot interval completes on first load and never fires again on subsequent reruns.

**What works:** A **permanent 250 ms setInterval** (never cleared) that:
1. Finds the tab list on each tick
2. Reads the saved tab index from `sessionStorage`
3. If the active tab doesn't match, clicks the right one (non-trusted click, won't overwrite the saved index)

Tab click saving uses **event delegation** on the stable `[data-testid="stApp"]` root (not on individual tab elements, which React may replace between reruns).

**Code location:** `dashboard/app.py` inside the `components.html(r"""...""")` block at the top of the file, after the CSS injection.

### 7. Dashboard CSS injection pattern

**Pattern:** A single `components.html(r"""<script>...</script>""", height=0)` at the top of the Streamlit script injects brand CSS, fonts, scroll/tab persistence JS, and element tagging into `window.parent.document`.

**Why `window.parent`:** Streamlit renders `components.html` in a sandboxed iframe. The actual app DOM is in the parent frame. All DOM operations must use `window.parent.document`.

**CSS is applied once per iframe load** (guarded by `if (!doc.getElementById('btl-css'))`). The tab/scroll JS runs indefinitely via setInterval.

**Guard pattern:** All "install once" operations use `if (!element.dataset.btlXxx)` data attributes on DOM elements to prevent double-wiring.

### 8. Pipeline expander hidden on desktop

**Problem:** Streamlit doesn't support Python-level responsive rendering. `st.markdown('<div class="foo">')` does not wrap around `st.expander()`.

**Solution:** The permanent 250 ms interval also tags the main-body pipeline expander with a CSS class (`btl-pipeline-main`). CSS rule hides it at ≥768 px. The sidebar always shows it on desktop; mobile gets it from the main body.

```javascript
main.querySelectorAll('[data-testid="stExpander"]').forEach(el => {
  const sum = el.querySelector('summary');
  if (sum && sum.textContent.includes('Pipeline controls')) {
    el.classList.add('btl-pipeline-main');
  }
});
```

### 9. Sidebar always-visible on desktop

**CSS pattern:**
```css
@media (min-width: 768px) {
  [data-testid="stSidebar"] { transform: none !important; min-width: 244px !important; ... }
  [data-testid="stSidebarCollapseButton"] { display: none !important; }
}
@media (max-width: 767px) {
  [data-testid="stSidebar"] { display: none !important; }
}
```
This overrides Streamlit's default slide-in/out transform. The collapse button is hidden since the sidebar is always open on desktop.

### 10. Dashboard authentication

Uses HMAC constant-time comparison (`hmac.compare_digest`) to prevent timing attacks. Rate-limited to 5 attempts before a 15-minute lockout. Password stored in Streamlit secrets (Railway env var `DASHBOARD_PASSWORD`). Session stored in `st.session_state`.

---

## What didn't work / lessons learned

| Thing tried | Problem | What to do instead |
|-------------|---------|-------------------|
| Instagram Graph API publishing | Reach consistently suppressed to 1–4 views vs 60–70+ for native posts | Route to Telegram; user posts natively |
| `setInterval` with `clearInterval` for tab restore | Interval completes on first iframe load; subsequent reruns don't reload the iframe (Streamlit caches it) | Permanent interval, never clear it |
| Attaching click listeners directly to `[data-baseweb="tab"]` elements | React may replace these nodes between reruns, losing the listeners | Event delegation on `[data-testid="stApp"]` |
| `st.markdown('<div class="foo">')` wrapping `st.expander()` | Streamlit components don't nest inside arbitrary HTML | Tag with JS in the polling interval; apply CSS class |
| Fetching Telegram chat ID via `getUpdates` API | Remote Railway/Claude env blocks outbound to `api.telegram.org` | User messages @userinfobot in Telegram to get their ID |
| Emoji in infographic text | Brand fonts lack emoji glyphs → tofu boxes (□) | `_strip_emojis()` regex covers emoji ranges |
| Smart punctuation in infographic text | `—`, `→`, `•`, `≥` etc. not in brand fonts → tofu boxes | `str.maketrans` map + `_NON_LATIN_RE` catch-all in `_strip_emojis()` |
| Separate `_sanitise_text` function | 54 call sites to update | Extended `_strip_emojis()` body instead — all callers unchanged |
| f-strings without placeholders | Ruff F541 error | Use raw strings `r"..."` for JS/HTML blocks; run `ruff check --fix` |

---

## Reproducing this system from scratch

### Core pipeline (minimum viable)

1. **Supabase:** Create `posts`, `topics`, `post_analytics`, `pipeline_commands` tables using the DDL in `core/database.py`. Create a public `media` storage bucket.
2. **Config:** All env vars in `core/config.py`. `Config.from_env()` builds the singleton. Every agent accepts `cfg: Config` — never read env vars directly in agents.
3. **Models:** `core/models.py` — `PostStatus` enum drives the entire lifecycle. A post must pass through `draft → content_ready → media_ready → scheduled → publishing → published`. `MANUAL_READY` is the Instagram Telegram branch.
4. **Scheduler:** APScheduler `BlockingScheduler` in `scheduler/cron.py`. One worker process. Add jobs with `CronTrigger` or `IntervalTrigger`. `run_pending_commands()` polls every 2 min for dashboard IPC.
5. **Publisher:** `publisher_agent.py` is the final step. Check platform, check `_is_instagram_api_mode()`, route accordingly. **Always check for `PostStatus.PUBLISHED` first** (idempotency — if already published, return immediately).

### Adding a new platform

1. Add to `Platform` enum in `core/models.py`
2. Add credentials to `Config` in `core/config.py`
3. Add a `_publish_<platform>` method in `publisher_agent.py`
4. Wire it into the dispatch dict
5. Add to `Config.configured_platforms()` check
6. Update `scheduler_agent.py` optimal-slot table

### Adding a new pipeline command

1. Write `run_<command>() -> str` in `scheduler/cron.py`
2. Add `elif command == "<command>": result_msg = run_<command>()` in `run_pending_commands()`
3. Add button in `dashboard/app.py` `_render_pipeline_controls()` that calls `_queue_command("<command>")`
4. Decide which collapsible section it belongs in (Create Content / Run Pipeline / Instagram / Maintenance)

### Dashboard button sections

Controls in `_render_pipeline_controls(scope)` use `st.expander`:
- **Always visible:** Pause/Resume Automation, Refresh data
- **📊 Create Content** (`expanded=True`): daily-use content generation buttons
- **⚙️ Run Pipeline** (`expanded=False`): research, generate, publish
- **📱 Instagram** (`expanded=False`): Telegram/API mode toggle
- **🔧 Maintenance** (`expanded=False`): image refresh, system check, token refresh

### Infographic reel visual themes

`infographic_agent.py` has multiple themes selectable via the dashboard. Each theme is a separate rendering function (`_render_wheel_slide`, `_render_dark_slide`, `_render_light_slide`, `_render_rich_slide`). All call `_strip_emojis()` on every text string before drawing.

Font loading is a 3-tier system in `core/image_utils.py`:
1. Load from `assets/fonts/` (bundled TTF)
2. Download from Google Fonts and cache
3. Fall back to PIL default font

### Testing

`tests/conftest.py` fakes every heavy SDK (anthropic, google.genai, supabase, apscheduler, requests_oauthlib) so the test suite runs with no API keys and no network. The `base_config` fixture provides a fully-populated `Config` with dummy credentials.

**Pattern for new tests:** monkeypatch `httpx.Client` to return `_FakeResponse` / `_FakeClient` objects. Never mock at the agent method level — test the full `publish()` / `run()` flow.

---

## Feature flags (Supabase Storage)

| Flag path | Active when | Controls |
|-----------|------------|----------|
| `config/automation_paused` | File exists | All scheduled jobs skip execution |
| `config/instagram.api_mode` | File exists | Instagram posts to Graph API instead of Telegram |

Check pattern:
```python
try:
    return get_storage().download("config/some_flag") is not None
except Exception:
    return False  # fail-open: assume flag is off
```

---

## Security notes

- **No hardcoded credentials.** All secrets via env vars through `Config`.
- **Prompt injection:** Custom topic input in dashboard is stripped of newlines and capped at 200 chars before reaching the LLM.
- **SSRF:** `quality_agent._fetch_image()` blocks non-HTTP schemes and private IP prefixes.
- **Caption validation:** Dashboard enforces ≤2,200 chars and ≤30 hashtags before DB write.
- **Auth:** Dashboard uses HMAC constant-time compare, 5-attempt rate limit, 15-min lockout.
- **SQL:** All DB access via Supabase Python client (parameterised). No raw SQL string interpolation.

---

## Deployment (Railway)

Two services:
1. **Worker:** `python scheduler/cron.py` — runs the APScheduler loop
2. **Dashboard:** `streamlit run dashboard/app.py` — the Streamlit web app

Both share the same env vars (set once in Railway, scoped to the project). The only persistent state is Supabase (DB + Storage).

Railway auto-redeploys on push to the deployed branch. The branch in use is `claude/automations-not-running-DGjgD` (development); merge to `main` to deploy.

---

## File size reference (approximate)

| File | Lines | Role |
|------|-------|------|
| `agents/infographic_agent.py` | ~3 000 | Largest — multiple visual themes, font handling |
| `dashboard/app.py` | ~2 600 | Dashboard — all tabs, controls, CSS/JS injection |
| `scheduler/cron.py` | ~2 050 | All pipeline jobs + command dispatch |
| `agents/analytics_agent.py` | ~1 050 | Platform engagement API calls |
| `agents/publisher_agent.py` | ~860 | Platform routing + API calls |
| `core/image_utils.py` | ~830 | Shared Pillow utilities |
| `agents/research_agent.py` | ~650 | Web search + topic scoring |
