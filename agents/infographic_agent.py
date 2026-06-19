"""Infographic Reel agent — produces data-driven AI/tech stat posts for Brite Tech Lifestyle.

Pipeline (per run):
  1. Pick or accept a topic (daily rotation if none supplied).
  2. Web-search for the latest statistics and facts on that topic.
  3. Ask Claude to synthesise the raw search results into exactly 4 eye-catching
     stat cards (big number + headline + context + source) plus caption / hashtags.
  4. Generate ONE Higgsfield 9:16 background image — reused for every card
     so one API credit produces the entire infographic.
  5. Composite 5 Pillow frames: title card + 4 stat cards.
     Each stat card uses the same background with a dark gradient overlay
     and a rotating vivid accent colour (cyan → magenta → orange → lime).
  6. Assemble the 5 frames into a 15-second MP4 (3 s per card) using the
     same moviepy + ffmpeg + Freesound path used by ReelsAgent.
  7. Upload to Supabase Storage and return ready-to-schedule Post objects.

Model-choice note:
  * Research step uses model_fast (Haiku)  — high-token web-search gather.
  * Planning step uses model_creative (Sonnet) — structured JSON synthesis.

Format note:
  create_posts() returns Post objects with post_type="reel".  The publisher
  handles them identically to ReelsAgent-generated Reels.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, Field

from core.config import Config, config
from core.models import Platform, Post, PostStatus

logger = logging.getLogger(__name__)

# ── Video constants (same as ReelsAgent) ──────────────────────────────────────
REEL_W, REEL_H = 1080, 1920
FPS = 24
CARD_DURATION = 3.0  # seconds per infographic frame
CROSSFADE_DUR = 0.4
MUSIC_VOLUME = 0.08

# ── Pillar → Higgsfield background prompt (9:16, vivid/dramatic) ─────────────
_BG_PROMPTS: dict[str, str] = {
    "AI Guide": (
        "dramatic abstract AI neural network, glowing cyan and electric-blue nodes connected by "
        "luminous data trails, deep dark cosmic background, 3D depth, cinematic, no text, "
        "no letters, no words, portrait 9:16"
    ),
    "Tech Lifestyle": (
        "sleek futuristic smart interior, holographic light panels, deep teal and gold ambient "
        "glow, minimalist luxury surfaces, dynamic reflections, no text, no letters, portrait 9:16"
    ),
    "Productivity": (
        "clean dark workspace with glowing screens, gradient from deep navy to violet, "
        "digital particles suggesting workflow automation, no text, no letters, portrait 9:16"
    ),
    "Fitness Tech": (
        "abstract health data visualization, vivid green and electric orange energy pulses, "
        "dark athletic background, motion blur, kinetic energy, no text, no letters, portrait 9:16"
    ),
    "Review": (
        "premium consumer electronics on dark matte surface, dramatic studio lighting, "
        "specular highlights, deep shadows, product photography, no text, no letters, portrait 9:16"
    ),
}
_BG_DEFAULT = (
    "futuristic digital abstract background, glowing geometric shapes, deep dark base with vivid "
    "neon accents in cyan and purple, cinematic, no text, no letters, no words, portrait 9:16"
)

# Accent colours for stat cards — one per card, cycles
_ACCENT_PALETTE = [
    (0, 229, 200),  # #00E5C8 cyan-mint
    (255, 65, 200),  # #FF41C8 magenta
    (255, 165, 0),  # #FFA500 amber-orange
    (110, 255, 80),  # #6EFF50 lime-green
]

# Daily topic rotation for the automated run
_AI_TOPICS = [
    "AI productivity tools adoption and ROI statistics 2026",
    "ChatGPT and generative AI business usage statistics 2026",
    "AI in healthcare: diagnosis accuracy and patient outcome statistics 2026",
    "Generative AI market size and investment growth 2026",
    "AI impact on jobs: automation, new roles and salary statistics 2026",
    "AI in education: student learning outcomes and adoption statistics 2026",
    "Smart home devices and AI assistant growth statistics 2026",
    "AI coding assistants: developer productivity statistics 2026",
    "AI customer service: chatbot adoption and satisfaction statistics 2026",
    "AI cybersecurity: threat detection and breach prevention statistics 2026",
    "Wearable tech and AI fitness tracking statistics 2026",
    "AI content creation: usage and engagement statistics 2026",
    "Self-driving and autonomous vehicle technology statistics 2026",
    "AI in finance: fraud detection and trading statistics 2026",
    "Remote work tech and AI collaboration tools statistics 2026",
]

_GRAPH = "https://platform.higgsfield.ai"
_FREESOUND_SEARCH = "https://freesound.org/apiv2/search/text/"
# Server-side search — the API runs the search between turns so the model
# never needs programmatic tool-calling support (Haiku 4.5 lacks it).
_WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "allowed_callers": ["direct"],
}
_MAX_WEB_CONTINUATIONS = 5

# Freesound fallback queries for infographic music (calm/inspiring feel)
_MUSIC_QUERIES = [
    "inspiring ambient background",
    "motivational ambient electronic",
    "uplifting ambient background music",
    "calm inspirational background",
    "ambient electronic inspiring",
]


# ── Pydantic models for structured Claude output ──────────────────────────────


class _StatCard(BaseModel):
    stat: str = Field(description="Key metric — e.g. '73%' or '$4.4T' or '1 in 3'. Max 8 chars.")
    headline: str = Field(description="What the stat means — max 7 words, punchy")
    context: str = Field(description="One line of extra context — max 12 words")
    source: str = Field(description="Source attribution — max 5 words, e.g. 'McKinsey 2026'")


class _InfographicPlan(BaseModel):
    topic_title: str = Field(description="Punchy topic name for the title card — max 5 words")
    hook: str = Field(description="Intriguing subheadline for the title card — max 10 words")
    caption: str = Field(description="Instagram caption — 2-3 warm, conversational sentences")
    hashtags: list[str] = Field(description="12 relevant hashtags without the # prefix")
    cards: list[_StatCard] = Field(description="Exactly 4 stat cards, most surprising stat first")


# ── Font paths (reuse core/image_utils paths) ──────────────────────────────────
_ASSETS = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets"))
_FONTS = os.path.join(_ASSETS, "fonts")
_FONT_HEADLINE = os.path.join(_FONTS, "BriteHero-Bold.ttf")
_FONT_BODY = os.path.join(_FONTS, "Figtree-Regular.ttf")
_FONT_TAGLINE = os.path.join(_FONTS, "PlayfairDisplay-Italic.ttf")


class InfographicAgent:
    """Generates data-driven AI/tech infographic Reels for Brite Tech Lifestyle."""

    def __init__(self, cfg: Config = config) -> None:
        if not (cfg.higgsfield_api_key or cfg.google_api_key):
            raise ValueError("Set HIGGSFIELD_API_KEY or GOOGLE_API_KEY")
        if not cfg.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY required for research synthesis")
        self._cfg = cfg
        self._freesound_key = cfg.freesound_api_key
        self._storage: object | None = None
        if cfg.supabase_url and cfg.supabase_key:
            try:
                from core.storage import get_storage

                self._storage = get_storage(cfg)
            except Exception:
                logger.warning("InfographicAgent: Supabase Storage unavailable", exc_info=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def create_posts(
        self,
        topic: str | None = None,
        platforms: list[str] | None = None,
    ) -> list[Post]:
        """Full pipeline: research → design → render → return Posts ready for scheduling.

        *platforms* defaults to ["instagram", "facebook"] when both are configured.
        Returns a list of Post objects (video_url set, status=CONTENT_READY).
        The caller is responsible for scheduling and persisting them.
        """
        if platforms is None:
            configured = set(self._cfg.configured_platforms())
            platforms = [
                p for p in [Platform.INSTAGRAM.value, Platform.FACEBOOK.value] if p in configured
            ]
            if not platforms:
                platforms = [Platform.INSTAGRAM.value]

        topic = topic or self._pick_topic()
        logger.info("InfographicAgent: starting infographic for topic=%r", topic)

        plan = self._research_and_plan(topic)
        logger.info("InfographicAgent: planned '%s' — %d cards", plan.topic_title, len(plan.cards))

        bg_bytes = self._generate_background(topic)
        frames = self._compose_all_frames(bg_bytes, plan)
        logger.info("InfographicAgent: composed %d frames", len(frames))

        video_url = self._build_and_upload_reel(frames, topic)
        if not video_url:
            raise RuntimeError("InfographicAgent: reel upload failed")

        caption = plan.caption
        hashtags = plan.hashtags
        title = plan.topic_title
        pillar = "AI Guide"

        posts: list[Post] = []
        for plat in platforms:
            post = Post(
                pillar=pillar,
                platform=plat,
                topic=topic,
                title=title,
                caption=caption,
                hashtags=hashtags,
                video_url=video_url,
                post_type="infographic_reel",
                status=PostStatus.CONTENT_READY.value,
            )
            posts.append(post)
            logger.info("InfographicAgent: created %s reel post %s", plat, post.id)

        return posts

    # ── Topic selection ────────────────────────────────────────────────────────

    def _pick_topic(self) -> str:
        """Pick today's topic using a date-stable rotation."""
        day_index = datetime.now(UTC).timetuple().tm_yday
        return _AI_TOPICS[day_index % len(_AI_TOPICS)]

    # ── Research & planning ────────────────────────────────────────────────────

    def _research_and_plan(self, topic: str) -> _InfographicPlan:
        """Web-search for stats then synthesise into a structured card plan."""
        import anthropic as _ant

        client = _ant.Anthropic(api_key=self._cfg.anthropic_api_key)

        # --- Step 1: web search for raw facts (server-side, Sonnet) -------------
        # Server-side search (allowed_callers=["direct"]) handles searches
        # automatically between turns; re-send on pause_turn to let it resume.
        # Using model_creative because Haiku 4.5 doesn't support programmatic
        # tool calling, which is required without the server-side flag.
        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"Search for the latest statistics and facts about: {topic}. "
                    "I need 5-8 specific, surprising, verified numbers from 2025-2026 "
                    "(percentages, dollar values, growth rates, time savings, adoption figures). "
                    "Include the source name and year for each stat. "
                    "Focus on facts that would make a non-technical person say 'wow'."
                ),
            }
        ]

        continuations = 0
        while continuations < _MAX_WEB_CONTINUATIONS:
            response = client.messages.create(
                model=self._cfg.model_creative,
                max_tokens=3000,
                tools=[_WEB_SEARCH_TOOL],
                messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})
            continuations += 1

        raw_research = "\n".join(
            getattr(b, "text", "") for b in response.content if hasattr(b, "text")
        )
        logger.debug("InfographicAgent: raw research (%d chars)", len(raw_research))

        # --- Step 2: structure into card plan via tool use (SDK-version-safe) ---
        # tool_choice={"type":"tool"} forces the model to always call the tool,
        # giving us a guaranteed structured JSON output without needing
        # beta.messages.parse() or response_format (added in newer SDK versions).
        plan_tool = {
            "name": "create_infographic_plan",
            "description": "Produce the structured infographic plan from research findings.",
            "input_schema": _InfographicPlan.model_json_schema(),
        }
        plan_response = client.messages.create(
            model=self._cfg.model_creative,
            max_tokens=1500,
            system=(
                "You are a social media infographic designer for Brite Tech Lifestyle — "
                "a brand that makes technology feel exciting and accessible. "
                "Given research notes, produce an infographic plan with exactly 4 stat cards. "
                "Lead with the most jaw-dropping statistic. Keep all text SHORT — "
                "stat cards live on a phone screen."
            ),
            tools=[plan_tool],
            tool_choice={"type": "tool", "name": "create_infographic_plan"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Topic: {topic}\n\nResearch findings:\n{raw_research}\n\n"
                        "Create an infographic plan for a 15-second Instagram Reel. "
                        "Each stat card needs a big memorable number, a 7-word max headline, "
                        "a 12-word max context line, and a source. "
                        "Write the Instagram caption in a warm, confident voice that "
                        "makes followers want to save the post."
                    ),
                }
            ],
        )
        for block in plan_response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and block.name == "create_infographic_plan"
            ):
                return _InfographicPlan(**block.input)
        raise RuntimeError("InfographicAgent: structured plan tool call returned no result")

    # ── Background generation ──────────────────────────────────────────────────

    def _generate_background(self, topic: str) -> bytes:
        """Generate one Higgsfield 9:16 background image for all cards."""
        if self._cfg.higgsfield_api_key:
            try:
                return self._higgsfield_background(topic)
            except Exception:
                logger.warning("InfographicAgent: Higgsfield failed; trying Imagen", exc_info=True)
        return self._imagen_background(topic)

    def _higgsfield_background(self, topic: str) -> bytes:
        prompt = _BG_DEFAULT
        for pillar_name, pillar_prompt in _BG_PROMPTS.items():
            if any(kw in topic.lower() for kw in pillar_name.lower().split()):
                prompt = pillar_prompt
                break

        headers = {
            "Authorization": f"Key {self._cfg.higgsfield_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as hx:
            resp = hx.post(
                f"{_GRAPH}/v1/text2image/soul",
                headers=headers,
                json={
                    "prompt": prompt,
                    "aspect_ratio": "9:16",
                    "quality": "HD",
                    "batch_size": "SINGLE",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        request_id = data.get("request_id") or data.get("id")
        if not request_id:
            raise RuntimeError(f"Higgsfield: no request_id in response: {data}")

        poll_url = f"{_GRAPH}/requests/{request_id}/status"
        headers_poll = {"Authorization": f"Key {self._cfg.higgsfield_api_key}"}
        deadline = time.time() + 300
        interval = 2.0
        while time.time() < deadline:
            time.sleep(interval)
            interval = min(interval * 1.5, 10)
            with httpx.Client(timeout=15) as hx:
                s = hx.get(poll_url, headers=headers_poll)
                s.raise_for_status()
                sd = s.json()
            status = sd.get("status", "")
            if status == "completed":
                images = sd.get("images") or []
                if not images:
                    raise RuntimeError("Higgsfield: completed but returned no images")
                image_url = images[0].get("url") or images[0]
                with httpx.Client(timeout=30) as hx:
                    ir = hx.get(image_url)
                    ir.raise_for_status()
                return ir.content
            if status in ("failed", "nsfw"):
                raise RuntimeError(f"Higgsfield generation {status}: {sd}")

        raise RuntimeError("Higgsfield timed out after 5 minutes")

    def _imagen_background(self, topic: str) -> bytes:
        if not self._cfg.google_api_key:
            raise RuntimeError("Neither Higgsfield nor Google API key configured")
        from google import genai
        from google.genai import types

        prompt = _BG_DEFAULT
        for pillar_name, pillar_prompt in _BG_PROMPTS.items():
            if any(kw in topic.lower() for kw in pillar_name.lower().split()):
                prompt = pillar_prompt
                break

        client = genai.Client(api_key=self._cfg.google_api_key)
        resp = client.models.generate_images(
            model=self._cfg.imagen_model,
            prompt=prompt,
            config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="9:16"),
        )
        images = getattr(resp, "generated_images", None) or []
        if not images:
            raise RuntimeError("Imagen returned no images")
        return images[0].image.image_bytes

    # ── Card composition ───────────────────────────────────────────────────────

    def _compose_all_frames(self, bg_bytes: bytes, plan: _InfographicPlan) -> list[bytes]:
        """Return PNG bytes for each Reel frame: [title, stat1, stat2, stat3, stat4]."""
        frames = [self._compose_title_card(bg_bytes, plan)]
        for i, card in enumerate(plan.cards[:4]):
            frames.append(self._compose_stat_card(bg_bytes, card, i))
        return frames

    def _compose_title_card(self, bg_bytes: bytes, plan: _InfographicPlan) -> bytes:
        from PIL import Image, ImageDraw

        bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB").resize((REEL_W, REEL_H), Image.LANCZOS)
        overlay = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))

        # Dark gradient — transparent top, heavy bottom
        grad = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))
        dg = ImageDraw.Draw(grad)
        fade_start = int(REEL_H * 0.25)
        for y in range(fade_start, REEL_H):
            t = (y - fade_start) / (REEL_H - fade_start)
            alpha = int(225 * (t**0.55))
            dg.line([(0, y), (REEL_W, y)], fill=(8, 8, 18, alpha))
        overlay = Image.alpha_composite(overlay, grad)

        # Accent colour bar at very bottom
        accent = _ACCENT_PALETTE[0]
        bar = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))
        ImageDraw.Draw(bar).rectangle(
            [(0, REEL_H - 14), (REEL_W, REEL_H)],
            fill=(*accent, 255),
        )
        overlay = Image.alpha_composite(overlay, bar)

        comp = bg.convert("RGBA")
        comp = Image.alpha_composite(comp, overlay)
        draw = ImageDraw.Draw(comp)

        pad = int(REEL_W * 0.07)
        max_w = REEL_W - 2 * pad

        # "BRITE TECH LIFESTYLE" small label at top
        from core.image_utils import _fit_lines, _load_font

        font_label = _load_font(_FONT_BODY, 28)
        draw.text(
            (pad, int(REEL_H * 0.06)),
            "BRITE TECH LIFESTYLE",
            font=font_label,
            fill=(*accent, 200),
        )

        # Main topic title — large, white
        font_hl, hl_lines, hl_sz = _fit_lines(
            draw,
            plan.topic_title.upper(),
            _FONT_HEADLINE,
            max(72, int(REEL_H * 0.072)),
            max(48, int(REEL_H * 0.048)),
            max_w,
            max_lines=3,
        )
        hl_lh = int(hl_sz * 1.05)

        # Hook subline — slightly below title
        font_hook, hook_lines, hook_sz = _fit_lines(
            draw,
            plan.hook,
            _FONT_TAGLINE,
            max(36, int(REEL_H * 0.036)),
            max(26, int(REEL_H * 0.026)),
            max_w,
            max_lines=2,
        )
        hook_lh = int(hook_sz * 1.3)

        block_h = hl_lh * len(hl_lines) + int(hook_sz * 0.7) + hook_lh * len(hook_lines)
        text_y = int(REEL_H * 0.72) - block_h // 2

        for line in hl_lines:
            draw.text((pad, text_y), line, font=font_hl, fill=(255, 255, 255, 255))
            text_y += hl_lh

        text_y += int(hook_sz * 0.7)
        for line in hook_lines:
            draw.text((pad, text_y), line, font=font_hook, fill=(210, 215, 228, 230))
            text_y += hook_lh

        # Progress dots — bottom centre
        self._draw_progress_dots(draw, 0, len(self._dummy_cards(4)))

        # Brand logo
        from core.image_utils import add_brand_overlay

        out_buf = io.BytesIO()
        comp.convert("RGB").save(out_buf, format="PNG")
        return add_brand_overlay(
            out_buf.getvalue(),
            self._cfg.brand_name,
            crop_bars=False,
            corner="top_right",
            logo_scale=1.3,
        )

    def _compose_stat_card(self, bg_bytes: bytes, card: _StatCard, card_index: int) -> bytes:
        from PIL import Image, ImageDraw

        accent = _ACCENT_PALETTE[card_index % len(_ACCENT_PALETTE)]

        bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB").resize((REEL_W, REEL_H), Image.LANCZOS)
        overlay = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))

        # Heavy dark overlay — almost full coverage so text pops
        dark = Image.new("RGBA", (REEL_W, REEL_H), (6, 6, 16, 200))
        overlay = Image.alpha_composite(overlay, dark)

        # Vivid accent strip on left edge
        strip = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))
        ImageDraw.Draw(strip).rectangle([(0, 0), (8, REEL_H)], fill=(*accent, 255))
        overlay = Image.alpha_composite(overlay, strip)

        # Bottom accent bar
        bar = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 0))
        ImageDraw.Draw(bar).rectangle([(0, REEL_H - 14), (REEL_W, REEL_H)], fill=(*accent, 255))
        overlay = Image.alpha_composite(overlay, bar)

        comp = bg.convert("RGBA")
        comp = Image.alpha_composite(comp, overlay)
        draw = ImageDraw.Draw(comp)

        from core.image_utils import _fit_lines, _load_font

        pad = int(REEL_W * 0.07)
        left = pad + 24  # offset past the accent strip
        max_w = REEL_W - left - pad

        # Card number badge — top left
        font_badge = _load_font(_FONT_BODY, 26)
        draw.text(
            (left, int(REEL_H * 0.06)),
            f"FACT {card_index + 1} OF 4",
            font=font_badge,
            fill=(*accent, 180),
        )

        # BIG stat number — centre of card
        font_stat, stat_lines, stat_sz = _fit_lines(
            draw,
            card.stat,
            _FONT_HEADLINE,
            max(160, int(REEL_H * 0.145)),
            max(100, int(REEL_H * 0.09)),
            max_w,
            max_lines=1,
        )
        stat_y = int(REEL_H * 0.30)
        for line in stat_lines:
            draw.text((left, stat_y), line, font=font_stat, fill=(*accent, 255))
            stat_y += int(stat_sz * 1.05)

        stat_y += int(REEL_H * 0.02)

        # Headline — white, bold
        font_hl, hl_lines, hl_sz = _fit_lines(
            draw,
            card.headline,
            _FONT_HEADLINE,
            max(52, int(REEL_H * 0.052)),
            max(36, int(REEL_H * 0.036)),
            max_w,
            max_lines=2,
        )
        hl_lh = int(hl_sz * 1.08)
        for line in hl_lines:
            draw.text((left, stat_y), line, font=font_hl, fill=(255, 255, 255, 255))
            stat_y += hl_lh

        stat_y += int(hl_sz * 0.5)

        # Context line — silver
        font_ctx, ctx_lines, ctx_sz = _fit_lines(
            draw,
            card.context,
            _FONT_BODY,
            max(34, int(REEL_H * 0.034)),
            max(24, int(REEL_H * 0.024)),
            max_w,
            max_lines=2,
        )
        ctx_lh = int(ctx_sz * 1.35)
        for line in ctx_lines:
            draw.text((left, stat_y), line, font=font_ctx, fill=(180, 185, 200, 220))
            stat_y += ctx_lh

        # Source attribution — bottom area
        font_src = _load_font(_FONT_TAGLINE, max(26, int(REEL_H * 0.026)))
        src_bbox = draw.textbbox((0, 0), f"Source: {card.source}", font=font_src)
        draw.text(
            (left, REEL_H - int(REEL_H * 0.10) - src_bbox[3]),
            f"Source: {card.source}",
            font=font_src,
            fill=(130, 135, 150, 200),
        )

        # Progress dots
        self._draw_progress_dots(draw, card_index + 1, 5)

        from core.image_utils import add_brand_overlay

        out_buf = io.BytesIO()
        comp.convert("RGB").save(out_buf, format="PNG")
        return add_brand_overlay(
            out_buf.getvalue(),
            self._cfg.brand_name,
            crop_bars=False,
            corner="top_right",
            logo_scale=1.3,
        )

    def _draw_progress_dots(self, draw, active: int, total: int) -> None:
        """Draw dot indicators at the bottom of the card."""

        dot_r = 6
        gap = 20
        total_w = total * (dot_r * 2) + (total - 1) * gap
        x_start = (REEL_W - total_w) // 2
        y = REEL_H - 60
        for i in range(total):
            x = x_start + i * (dot_r * 2 + gap) + dot_r
            accent = _ACCENT_PALETTE[max(0, i - 1) % len(_ACCENT_PALETTE)]
            fill = (*accent, 255) if i == active else (80, 85, 100, 180)
            draw.ellipse([(x - dot_r, y - dot_r), (x + dot_r, y + dot_r)], fill=fill)

    @staticmethod
    def _dummy_cards(n: int) -> list:
        return [None] * n

    # ── Video assembly ─────────────────────────────────────────────────────────

    def _build_and_upload_reel(self, frames: list[bytes], topic: str) -> str | None:
        """Assemble frames into an MP4, mix music, upload to Supabase, return URL."""
        temp_files: list[str] = []
        try:
            frame_paths = []
            for i, frame_bytes in enumerate(frames):
                tmp = tempfile.NamedTemporaryFile(
                    delete=False, suffix=".png", prefix=f"infog_frame_{i}_"
                )
                tmp.write(frame_bytes)
                tmp.close()
                frame_paths.append(tmp.name)
            temp_files.extend(frame_paths)

            video_path = self._build_slideshow(frame_paths)
            temp_files.append(video_path)

            music_path = self._fetch_music()
            if music_path:
                temp_files.append(music_path)
                mixed_path = self._mix_audio(video_path, music_path)
                temp_files.append(mixed_path)
                video_path = mixed_path

            return self._upload_video(video_path, topic)

        except Exception:
            logger.exception("InfographicAgent: reel assembly failed")
            return None

        finally:
            for p in temp_files:
                try:
                    if p and os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass

    def _build_slideshow(self, frame_paths: list[str]) -> str:
        from moviepy.editor import ImageClip, concatenate_videoclips

        clips = []
        for i, path in enumerate(frame_paths):
            clip = ImageClip(path).set_duration(CARD_DURATION)
            if i > 0:
                clip = clip.crossfadein(CROSSFADE_DUR)
            clips.append(clip)

        video = concatenate_videoclips(clips, padding=-CROSSFADE_DUR, method="compose")
        out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", prefix="infog_silent_")
        out.close()
        video.write_videofile(
            out.name,
            fps=FPS,
            codec="libx264",
            audio=False,
            ffmpeg_params=["-pix_fmt", "yuv420p", "-profile:v", "baseline", "-level", "3.0"],
            logger=None,
        )
        video.close()
        return out.name

    def _mix_audio(self, video_path: str, music_path: str) -> str:
        ffmpeg = self._ffmpeg_exe()
        out = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", prefix="infog_audio_")
        out.close()
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            video_path,
            "-stream_loop",
            "-1",
            "-i",
            music_path,
            "-filter_complex",
            f"[1:a]volume={MUSIC_VOLUME}[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            out.name,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg audio mix failed: {result.stderr.decode(errors='replace')[-300:]}"
            )
        return out.name

    @staticmethod
    def _ffmpeg_exe() -> str:
        import shutil

        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:
            raise RuntimeError("ffmpeg not found on PATH or via imageio-ffmpeg") from exc

    def _fetch_music(self) -> str | None:
        if not self._freesound_key:
            return None
        with httpx.Client(timeout=20.0) as client:
            for query in _MUSIC_QUERIES:
                try:
                    resp = client.get(
                        _FREESOUND_SEARCH,
                        params={
                            "query": query,
                            "filter": "duration:[20 TO 120]",
                            "fields": "id,name,previews,duration,license",
                            "sort": "rating_desc",
                            "page_size": "5",
                            "token": self._freesound_key,
                        },
                    )
                    resp.raise_for_status()
                    results = resp.json().get("results", [])
                    if not results:
                        continue

                    track = results[0]
                    preview_url = track.get("previews", {}).get("preview-hq-mp3") or track.get(
                        "previews", {}
                    ).get("preview-lq-mp3")
                    if not preview_url:
                        continue

                    audio_resp = client.get(preview_url, timeout=30.0)
                    audio_resp.raise_for_status()
                    content = audio_resp.content
                    if not (
                        content[:3] == b"ID3"
                        or (len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0)
                    ):
                        logger.warning("InfographicAgent: Freesound returned non-MP3 bytes")
                        continue

                    tmp = tempfile.NamedTemporaryFile(
                        delete=False, suffix=".mp3", prefix="infog_music_"
                    )
                    tmp.write(content)
                    tmp.close()
                    logger.info(
                        "InfographicAgent: music from query=%r track=%r licence=%s",
                        query,
                        track.get("name", "?"),
                        track.get("license", "?"),
                    )
                    return tmp.name

                except Exception:
                    logger.debug("InfographicAgent: music query %r failed", query, exc_info=True)

        logger.warning("InfographicAgent: no music found — producing silent Reel")
        return None

    # ── Upload ─────────────────────────────────────────────────────────────────

    def _upload_video(self, video_path: str, topic: str) -> str | None:
        with open(video_path, "rb") as fh:
            video_bytes = fh.read()

        slug = topic[:40].lower().replace(" ", "_").replace("/", "_")
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        storage_path = f"reels/infographic_{slug}_{ts}.mp4"

        if self._storage is not None:
            try:
                return self._storage.upload(storage_path, video_bytes, content_type="video/mp4")
            except Exception:
                logger.exception("InfographicAgent: Supabase upload failed")

        # Local fallback
        local_dir = os.path.join(tempfile.gettempdir(), "btl_infographics")
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, os.path.basename(storage_path))
        with open(local_path, "wb") as fh:
            fh.write(video_bytes)
        logger.warning("InfographicAgent: saved locally to %s (no Supabase)", local_path)
        return local_path
