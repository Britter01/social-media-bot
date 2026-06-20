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
import math
import os
import re
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


# ── Emoji stripping ───────────────────────────────────────────────────────────
# Brand fonts (BriteHero-Bold, Figtree, PlayfairDisplay) lack emoji glyphs,
# causing Pillow to render them as tofu squares (□). Strip before drawing.
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f9ff"
    "\U00002702-\U000027b0"
    "\U000024c2-\U0001f251"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "☀-⛿"
    "✀-➿"
    "︀-️"
    "‍"
    "]+",
    re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()


# ── Pydantic models for structured Claude output ──────────────────────────────


class _StatCard(BaseModel):
    stat: str = Field(description="Key metric — e.g. '73%' or '$4.4T' or '1 in 3'. Max 8 chars.")
    headline: str = Field(description="What the stat means — max 7 words, punchy")
    context: str = Field(description="One line of extra context — max 12 words")
    source: str = Field(description="Source attribution — max 5 words, e.g. 'McKinsey 2026'")


class _InfographicPlan(BaseModel):
    topic_title: str = Field(
        description="Punchy topic name for the title card — max 5 words, no emojis"
    )
    hook: str = Field(
        description="Intriguing subheadline for the title card — max 10 words, no emojis"
    )
    caption: str = Field(description="Instagram caption — 2-3 warm, conversational sentences")
    hashtags: list[str] = Field(description="12 relevant hashtags without the # prefix")
    cards: list[_StatCard] = Field(description="Exactly 4 stat cards, most surprising stat first")


class _TipItem(BaseModel):
    title: str = Field(description="Short actionable tip/step title — max 6 words, no emojis")
    body: str = Field(description="1-2 sentences of explanation — max 20 words, no emojis")


class _TipsPlan(BaseModel):
    topic_title: str = Field(description="Punchy topic name — max 5 words, no emojis")
    hook: str = Field(description="Intriguing subheadline — max 10 words, no emojis")
    caption: str = Field(description="Instagram caption — 2-3 warm, conversational sentences")
    hashtags: list[str] = Field(description="12 relevant hashtags without the # prefix")
    items: list[_TipItem] = Field(
        description="Actionable tips or steps (exactly 6 for wheel/dark styles, 10 for light style)"
    )


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
        fmt: str = "reel",
    ) -> list[Post]:
        """Full pipeline: research → design → render → return Posts ready for scheduling.

        *fmt* is "reel" (default), "static", "wheel", "dark", or "light".
        *platforms* defaults to ["instagram", "facebook"] for reels, ["instagram"] for others.
        Returns Post objects with status=CONTENT_READY; caller schedules and persists them.
        """
        topic = topic or self._pick_topic()
        logger.info("InfographicAgent: starting %s infographic for topic=%r", fmt, topic)

        if fmt in ("wheel", "dark", "light"):
            n_items = 10 if fmt == "light" else 6
            tips_plan = self._research_and_plan_tips(topic, n_items)
            logger.info(
                "InfographicAgent: tips plan '%s' — %d items",
                tips_plan.topic_title,
                len(tips_plan.items),
            )
            return self._create_tips_posts(topic, tips_plan, platforms, fmt)

        plan = self._research_and_plan(topic)
        logger.info("InfographicAgent: planned '%s' — %d cards", plan.topic_title, len(plan.cards))

        if fmt == "static":
            return self._create_static_posts(topic, plan, platforms)
        return self._create_reel_posts(topic, plan, platforms)

    def _create_reel_posts(
        self, topic: str, plan: _InfographicPlan, platforms: list[str] | None
    ) -> list[Post]:
        if platforms is None:
            configured = set(self._cfg.configured_platforms())
            platforms = [
                p for p in [Platform.INSTAGRAM.value, Platform.FACEBOOK.value] if p in configured
            ] or [Platform.INSTAGRAM.value]

        bg_bytes = self._generate_background(topic)
        frames = self._compose_all_frames(bg_bytes, plan)
        logger.info("InfographicAgent: composed %d reel frames", len(frames))

        video_url = self._build_and_upload_reel(frames, topic)
        if not video_url:
            raise RuntimeError("InfographicAgent: reel upload failed")

        posts: list[Post] = []
        for plat in platforms:
            post = Post(
                pillar="AI Guide",
                platform=plat,
                topic=topic,
                title=plan.topic_title,
                caption=plan.caption,
                hashtags=plan.hashtags,
                video_url=video_url,
                post_type="infographic_reel",
                status=PostStatus.CONTENT_READY.value,
            )
            posts.append(post)
            logger.info("InfographicAgent: created %s infographic_reel post %s", plat, post.id)
        return posts

    def _create_static_posts(
        self, topic: str, plan: _InfographicPlan, platforms: list[str] | None
    ) -> list[Post]:
        if platforms is None:
            platforms = [Platform.INSTAGRAM.value]

        bg_bytes = self._generate_background(topic, aspect_ratio="1:1")
        image_bytes = self._compose_static_image(bg_bytes, plan)
        logger.info("InfographicAgent: composed static image (%d bytes)", len(image_bytes))

        image_url = self._upload_image(image_bytes, topic)
        if not image_url:
            raise RuntimeError("InfographicAgent: static image upload failed")

        posts: list[Post] = []
        for plat in platforms:
            post = Post(
                pillar="AI Guide",
                platform=plat,
                topic=topic,
                title=plan.topic_title,
                caption=plan.caption,
                hashtags=plan.hashtags,
                thumbnail_url=image_url,
                post_type="infographic_static",
                status=PostStatus.CONTENT_READY.value,
            )
            posts.append(post)
            logger.info("InfographicAgent: created %s infographic_static post %s", plat, post.id)
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

    def _generate_background(self, topic: str, aspect_ratio: str = "9:16") -> bytes:
        """Generate one Higgsfield background image for all cards."""
        if self._cfg.higgsfield_api_key:
            try:
                return self._higgsfield_background(topic, aspect_ratio)
            except Exception:
                logger.warning("InfographicAgent: Higgsfield failed; trying Imagen", exc_info=True)
        return self._imagen_background(topic, aspect_ratio)

    def _higgsfield_background(self, topic: str, aspect_ratio: str = "9:16") -> bytes:
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
                    "aspect_ratio": aspect_ratio,
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

    def _imagen_background(self, topic: str, aspect_ratio: str = "9:16") -> bytes:
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
            config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio=aspect_ratio),
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

        # Website label at top
        from core.image_utils import _fit_lines, _load_font

        font_label = _load_font(_FONT_BODY, 28)
        draw.text(
            (pad, int(REEL_H * 0.06)),
            "britetechlifestyle.com",
            font=font_label,
            fill=(*accent, 200),
        )

        # Main topic title — large, white
        font_hl, hl_lines, hl_sz = _fit_lines(
            draw,
            _strip_emojis(plan.topic_title).upper(),
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
            _strip_emojis(plan.hook),
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
            logo_top_pad=80,
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
            _strip_emojis(card.stat),
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
            _strip_emojis(card.headline),
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
            _strip_emojis(card.context),
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
            logo_top_pad=80,
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
        """Delegate to ReelsAgent's proven Freesound implementation.

        ReelsAgent's _fetch_music handles follow_redirects, magic-byte
        validation, and broad fallback queries — all confirmed working.
        Reusing it avoids duplicating that logic here.
        """
        try:
            from agents.reels_agent import ReelsAgent

            return ReelsAgent()._fetch_music("AI Guide")
        except Exception:
            logger.warning("InfographicAgent: music fetch failed", exc_info=True)
            return None

    # ── Static image composition ───────────────────────────────────────────────

    def _compose_static_image(self, bg_bytes: bytes, plan: _InfographicPlan) -> bytes:
        """Compose a single 1080×1080 infographic grid with all 4 stats."""
        from PIL import Image, ImageDraw

        from core.image_utils import _fit_lines, _load_font, add_brand_overlay

        W = H = 1080
        PAD = 28
        HEADER_H = 195
        GAP = 14
        CELL_W = (W - PAD * 2 - GAP) // 2  # 504
        CELL_H = (H - HEADER_H - PAD - GAP) // 2  # ≈ 418

        bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB").resize((W, H), Image.LANCZOS)
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        # Strong dark overlay so text is always readable
        dark = Image.new("RGBA", (W, H), (6, 6, 16, 215))
        overlay = Image.alpha_composite(overlay, dark)

        comp = bg.convert("RGBA")
        comp = Image.alpha_composite(comp, overlay)
        draw = ImageDraw.Draw(comp)

        # ── Header ─────────────────────────────────────────────────────────────
        accent0 = _ACCENT_PALETTE[0]

        font_label = _load_font(_FONT_BODY, 22)
        draw.text((PAD, PAD), "britetechlifestyle.com", font=font_label, fill=(*accent0, 200))

        font_hl, hl_lines, hl_sz = _fit_lines(
            draw,
            _strip_emojis(plan.topic_title).upper(),
            _FONT_HEADLINE,
            max(58, int(H * 0.058)),
            max(40, int(H * 0.038)),
            W - PAD * 2,
            max_lines=2,
        )
        hl_lh = int(hl_sz * 1.05)
        text_y = PAD + 36
        for line in hl_lines:
            draw.text((PAD, text_y), line, font=font_hl, fill=(255, 255, 255, 255))
            text_y += hl_lh

        font_hook, hook_lines, hook_sz = _fit_lines(
            draw,
            _strip_emojis(plan.hook),
            _FONT_TAGLINE,
            max(26, int(H * 0.026)),
            max(20, int(H * 0.020)),
            W - PAD * 2,
            max_lines=1,
        )
        draw.text(
            (PAD, text_y + 6),
            hook_lines[0] if hook_lines else "",
            font=font_hook,
            fill=(190, 195, 210, 220),
        )

        # Thin accent divider line below header
        line_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(line_layer).line(
            [(PAD, HEADER_H - 4), (W - PAD, HEADER_H - 4)],
            fill=(*accent0, 120),
            width=2,
        )
        comp = Image.alpha_composite(comp, line_layer)
        draw = ImageDraw.Draw(comp)

        # ── 2×2 stat grid ──────────────────────────────────────────────────────
        cards = (plan.cards + [None, None, None, None])[:4]
        for idx, card in enumerate(cards):
            if card is None:
                continue
            col = idx % 2
            row = idx // 2
            accent = _ACCENT_PALETTE[idx % len(_ACCENT_PALETTE)]

            x0 = PAD + col * (CELL_W + GAP)
            y0 = HEADER_H + row * (CELL_H + GAP)
            x1 = x0 + CELL_W
            y1 = y0 + CELL_H

            # Semi-transparent tinted cell background
            cell_bg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(cell_bg).rectangle(
                [(x0, y0), (x1, y1)],
                fill=(*accent, 18),  # very faint accent tint
            )
            comp = Image.alpha_composite(comp, cell_bg)

            # Left accent bar
            bar = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(bar).rectangle(
                [(x0, y0), (x0 + 6, y1)],
                fill=(*accent, 255),
            )
            comp = Image.alpha_composite(comp, bar)
            draw = ImageDraw.Draw(comp)

            inner_x = x0 + 18
            inner_w = CELL_W - 24
            cy = y0 + 18

            # Big stat number
            font_stat, stat_lines, stat_sz = _fit_lines(
                draw,
                _strip_emojis(card.stat),
                _FONT_HEADLINE,
                max(68, int(H * 0.068)),
                max(44, int(H * 0.044)),
                inner_w,
                max_lines=1,
            )
            for line in stat_lines:
                draw.text((inner_x, cy), line, font=font_stat, fill=(*accent, 255))
                cy += int(stat_sz * 1.05)

            cy += 6

            # Headline
            font_hl2, hl2_lines, hl2_sz = _fit_lines(
                draw,
                _strip_emojis(card.headline),
                _FONT_HEADLINE,
                max(28, int(H * 0.028)),
                max(20, int(H * 0.020)),
                inner_w,
                max_lines=2,
            )
            for line in hl2_lines:
                draw.text((inner_x, cy), line, font=font_hl2, fill=(255, 255, 255, 255))
                cy += int(hl2_sz * 1.1)

            cy += 4

            # Context
            font_ctx2, ctx2_lines, ctx2_sz = _fit_lines(
                draw,
                _strip_emojis(card.context),
                _FONT_BODY,
                max(20, int(H * 0.020)),
                max(15, int(H * 0.015)),
                inner_w,
                max_lines=2,
            )
            for line in ctx2_lines:
                draw.text((inner_x, cy), line, font=font_ctx2, fill=(165, 170, 185, 210))
                cy += int(ctx2_sz * 1.3)

            # Source — pinned near bottom of cell
            font_src2 = _load_font(_FONT_TAGLINE, max(17, int(H * 0.017)))
            src_bbox = draw.textbbox((0, 0), card.source, font=font_src2)
            draw.text(
                (inner_x, y1 - 16 - src_bbox[3]),
                card.source,
                font=font_src2,
                fill=(110, 115, 130, 190),
            )

        out_buf = io.BytesIO()
        comp.convert("RGB").save(out_buf, format="PNG")
        return add_brand_overlay(
            out_buf.getvalue(),
            self._cfg.brand_name,
            crop_bars=False,
            corner="bottom_right",
            logo_scale=1.2,
        )

    def _upload_image(self, image_bytes: bytes, topic: str) -> str | None:
        slug = topic[:40].lower().replace(" ", "_").replace("/", "_")
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        storage_path = f"thumbnails/infographic_{slug}_{ts}.png"

        if self._storage is not None:
            try:
                return self._storage.upload(storage_path, image_bytes, content_type="image/png")
            except Exception:
                logger.exception("InfographicAgent: image upload failed")

        local_dir = os.path.join(tempfile.gettempdir(), "btl_infographics")
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, os.path.basename(storage_path))
        with open(local_path, "wb") as fh:
            fh.write(image_bytes)
        logger.warning("InfographicAgent: image saved locally to %s (no Supabase)", local_path)
        return local_path

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

    # ── Tips-based research & planning ─────────────────────────────────────────

    def _research_and_plan_tips(self, topic: str, n_items: int = 6) -> _TipsPlan:
        """Web-search then synthesise into a structured tips/steps plan."""
        import anthropic as _ant

        client = _ant.Anthropic(api_key=self._cfg.anthropic_api_key)

        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"Search for the latest actionable tips, steps, or insights about: {topic}. "
                    f"I need {n_items} clear, practical, numbered points from 2025-2026. "
                    "Each should have a short punchy title and a 1-2 sentence explanation. "
                    "Focus on tips that would surprise or genuinely help a non-technical person."
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

        plan_tool = {
            "name": "create_tips_plan",
            "description": "Produce a structured infographic tips plan from research.",
            "input_schema": _TipsPlan.model_json_schema(),
        }
        plan_response = client.messages.create(
            model=self._cfg.model_creative,
            max_tokens=1500,
            system=(
                "You are a social media infographic designer for Brite Tech Lifestyle — "
                "a brand that makes technology feel exciting and accessible. "
                f"Given research notes, produce exactly {n_items} actionable tip items. "
                "Keep titles SHORT (max 6 words) and bodies BRIEF (max 20 words). "
                "No emojis anywhere — brand fonts cannot render them."
            ),
            tools=[plan_tool],
            tool_choice={"type": "tool", "name": "create_tips_plan"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Topic: {topic}\n\nResearch:\n{raw_research}\n\n"
                        f"Create a tips infographic plan with exactly {n_items} items. "
                        "Write the caption in a warm, confident voice."
                    ),
                }
            ],
        )
        for block in plan_response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "create_tips_plan":
                return _TipsPlan(**block.input)
        raise RuntimeError("InfographicAgent: tips plan tool call returned no result")

    def _create_tips_posts(
        self,
        topic: str,
        plan: _TipsPlan,
        platforms: list[str] | None,
        style: str,
    ) -> list[Post]:
        """Compose and upload a tips-style static infographic, return Post objects."""
        if platforms is None:
            platforms = [Platform.INSTAGRAM.value]

        if style == "wheel":
            bg_bytes = self._generate_background(topic, aspect_ratio="1:1")
            image_bytes = self._compose_wheel_image(bg_bytes, plan)
            post_type = "infographic_wheel"
        elif style == "dark":
            bg_bytes = self._generate_background(topic, aspect_ratio="1:1")
            image_bytes = self._compose_dark_panels_image(bg_bytes, plan)
            post_type = "infographic_dark"
        else:  # light
            image_bytes = self._compose_light_magazine_image(plan)
            post_type = "infographic_light"

        logger.info("InfographicAgent: composed %s image (%d bytes)", style, len(image_bytes))

        image_url = self._upload_image(image_bytes, topic)
        if not image_url:
            raise RuntimeError(f"InfographicAgent: {style} image upload failed")

        posts: list[Post] = []
        for plat in platforms:
            post = Post(
                pillar="AI Guide",
                platform=plat,
                topic=topic,
                title=plan.topic_title,
                caption=plan.caption,
                hashtags=plan.hashtags,
                thumbnail_url=image_url,
                post_type=post_type,
                status=PostStatus.CONTENT_READY.value,
            )
            posts.append(post)
            logger.info("InfographicAgent: created %s %s post %s", plat, post_type, post.id)
        return posts

    # ── Tips-style composition ─────────────────────────────────────────────────

    def _compose_wheel_image(self, bg_bytes: bytes, plan: _TipsPlan) -> bytes:
        """1080×1080 wheel/radial diagram with numbered tip panels (Brite style)."""
        from PIL import Image, ImageDraw, ImageFilter

        from core.image_utils import _fit_lines, _load_font, add_brand_overlay

        W = H = 1080
        PAD = 28

        bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB").resize((W, H), Image.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=14))
        dark = Image.new("RGBA", (W, H), (8, 8, 22, 175))
        comp = Image.alpha_composite(bg.convert("RGBA"), dark)
        draw = ImageDraw.Draw(comp)

        # ── Header ─────────────────────────────────────────────────────────────
        TITLE_H = 128
        accent0 = (90, 160, 255)

        font_lbl = _load_font(_FONT_BODY, 20)
        draw.text((PAD, PAD), "britetechlifestyle.com", font=font_lbl, fill=(*accent0, 190))

        font_hl, hl_lines, hl_sz = _fit_lines(
            draw,
            _strip_emojis(plan.topic_title).upper(),
            _FONT_HEADLINE,
            max(44, int(H * 0.044)),
            max(30, int(H * 0.028)),
            W - PAD * 2,
            max_lines=2,
        )
        ty = PAD + 28
        for line in hl_lines:
            draw.text((PAD, ty), line, font=font_hl, fill=(255, 255, 255, 255))
            ty += int(hl_sz * 1.05)

        # ── Wheel geometry ─────────────────────────────────────────────────────
        MAIN_TOP = TITLE_H + 8
        FOOT_H = 72
        MAIN_H = H - MAIN_TOP - FOOT_H

        PANEL_W = 268
        cx = W // 2
        cy = MAIN_TOP + MAIN_H // 2
        OUTER_R = 205
        INNER_R = 62

        wheel_colors = [
            (80, 130, 255),
            (255, 100, 50),
            (80, 210, 90),
            (175, 60, 220),
            (0, 195, 210),
            (240, 190, 0),
        ]

        n = min(6, len(plan.items))
        sector_deg = 360.0 / n
        for i in range(n):
            sa = i * sector_deg - 90
            ea = sa + sector_deg - 3
            c = wheel_colors[i % len(wheel_colors)]
            bbox = [cx - OUTER_R, cy - OUTER_R, cx + OUTER_R, cy + OUTER_R]
            draw.pieslice(bbox, start=sa, end=ea, fill=(*c, 225))

            # Number label inside sector at 70% radius
            mid_rad = math.radians(sa + sector_deg / 2)
            nx = cx + int(OUTER_R * 0.70 * math.cos(mid_rad))
            ny = cy + int(OUTER_R * 0.70 * math.sin(mid_rad))
            fn = _load_font(_FONT_HEADLINE, 24)
            draw.text((nx, ny), str(i + 1), font=fn, fill=(255, 255, 255, 240), anchor="mm")

        # Inner donut
        ib = [cx - INNER_R, cy - INNER_R, cx + INNER_R, cy + INNER_R]
        draw.ellipse(ib, fill=(10, 10, 28, 255))
        fc = _load_font(_FONT_BODY, 15)
        words = _strip_emojis(plan.topic_title).split()[:2]
        cy_t = cy - (len(words) - 1) * 9
        for w in words:
            draw.text((cx, cy_t), w.upper(), font=fc, fill=(190, 205, 230, 220), anchor="mm")
            cy_t += 18

        # ── Side panels ────────────────────────────────────────────────────────
        panel_h = MAIN_H // 3
        font_pt = _load_font(_FONT_HEADLINE, 18)
        font_pb = _load_font(_FONT_BODY, 14)

        for i in range(min(6, n)):
            tip = plan.items[i]
            c = wheel_colors[i % len(wheel_colors)]
            row = i % 3
            py0 = MAIN_TOP + row * panel_h + 6
            py1 = py0 + panel_h - 12

            if i < 3:
                px0, px1 = 4, PANEL_W - 4
            else:
                px0, px1 = W - PANEL_W + 4, W - 4

            pl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(pl).rounded_rectangle(
                [(px0, py0), (px1, py1)], radius=8, fill=(*c, 28), outline=(*c, 100), width=2
            )
            comp = Image.alpha_composite(comp, pl)
            draw = ImageDraw.Draw(comp)

            # Number circle
            nx2 = px0 + 18
            ny2 = py0 + 18
            draw.ellipse([(nx2 - 11, ny2 - 11), (nx2 + 11, ny2 + 11)], fill=(*c, 200))
            draw.text(
                (nx2, ny2),
                str(i + 1),
                font=_load_font(_FONT_HEADLINE, 14),
                fill=(255, 255, 255, 255),
                anchor="mm",
            )

            # Tip title
            tx = px0 + 36
            inner_w = (px1 - px0) - 42
            _, t_lines, t_sz = _fit_lines(
                draw, _strip_emojis(tip.title), _FONT_HEADLINE, 18, 14, inner_w, max_lines=2
            )
            ty2 = py0 + 8
            for line in t_lines:
                draw.text((tx, ty2), line, font=font_pt, fill=(255, 255, 255, 240))
                ty2 += int(t_sz * 1.1)

            # Tip body
            _, b_lines, b_sz = _fit_lines(
                draw, _strip_emojis(tip.body), _FONT_BODY, 14, 12, (px1 - px0) - 12, max_lines=3
            )
            ty2 += 3
            for line in b_lines:
                draw.text((px0 + 8, ty2), line, font=font_pb, fill=(170, 180, 200, 195))
                ty2 += int(b_sz * 1.25)

        # ── Footer ─────────────────────────────────────────────────────────────
        fh2 = _load_font(_FONT_BODY, 20)
        draw.text((PAD, H - 44), "@britetechlifestyle", font=fh2, fill=(140, 155, 180, 190))

        out = io.BytesIO()
        comp.convert("RGB").save(out, format="PNG")
        return add_brand_overlay(
            out.getvalue(),
            self._cfg.brand_name,
            crop_bars=False,
            corner="bottom_right",
            logo_scale=1.2,
        )

    def _compose_dark_panels_image(self, bg_bytes: bytes, plan: _TipsPlan) -> bytes:
        """1080×1080 dark numbered panels (like screenshot 3 style)."""
        from PIL import Image, ImageDraw

        from core.image_utils import _fit_lines, _load_font, add_brand_overlay

        W = H = 1080
        PAD = 28
        HEADER_H = 185
        GAP = 10

        bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB").resize((W, H), Image.LANCZOS)
        dark = Image.new("RGBA", (W, H), (6, 8, 22, 210))
        comp = Image.alpha_composite(bg.convert("RGBA"), dark)
        draw = ImageDraw.Draw(comp)

        # ── Header ─────────────────────────────────────────────────────────────
        accent0 = _ACCENT_PALETTE[0]

        font_lbl = _load_font(_FONT_BODY, 22)
        draw.text((PAD, PAD), "britetechlifestyle.com", font=font_lbl, fill=(*accent0, 200))

        font_hl, hl_lines, hl_sz = _fit_lines(
            draw,
            _strip_emojis(plan.topic_title).upper(),
            _FONT_HEADLINE,
            max(56, int(H * 0.056)),
            max(38, int(H * 0.036)),
            W - PAD * 2,
            max_lines=2,
        )
        ty = PAD + 32
        for line in hl_lines:
            draw.text((PAD, ty), line, font=font_hl, fill=(255, 255, 255, 255))
            ty += int(hl_sz * 1.05)

        font_hook, hook_lines, _ = _fit_lines(
            draw,
            _strip_emojis(plan.hook),
            _FONT_TAGLINE,
            max(26, int(H * 0.025)),
            max(20, int(H * 0.018)),
            W - PAD * 2,
            max_lines=1,
        )
        draw.text(
            (PAD, ty + 4),
            hook_lines[0] if hook_lines else "",
            font=font_hook,
            fill=(185, 192, 210, 210),
        )

        # Divider
        dl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(dl).line(
            [(PAD, HEADER_H - 6), (W - PAD, HEADER_H - 6)], fill=(*accent0, 100), width=2
        )
        comp = Image.alpha_composite(comp, dl)
        draw = ImageDraw.Draw(comp)

        # ── 2×3 panel grid ─────────────────────────────────────────────────────
        FOOT_H = 60
        GRID_H = H - HEADER_H - FOOT_H
        n_cols, n_rows = 2, 3
        CELL_W = (W - PAD * 2 - GAP) // n_cols
        CELL_H = (GRID_H - GAP * (n_rows - 1)) // n_rows

        items = (plan.items + [None] * 6)[:6]
        font_num_b = _load_font(_FONT_HEADLINE, 22)
        font_title = _load_font(_FONT_HEADLINE, 22)
        font_body = _load_font(_FONT_BODY, 16)

        for idx, tip in enumerate(items):
            if tip is None:
                continue
            col = idx % n_cols
            row = idx // n_cols
            accent = _ACCENT_PALETTE[idx % len(_ACCENT_PALETTE)]

            x0 = PAD + col * (CELL_W + GAP)
            y0 = HEADER_H + row * (CELL_H + GAP)
            x1 = x0 + CELL_W
            y1 = y0 + CELL_H

            # Cell background
            cb = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(cb).rectangle([(x0, y0), (x1, y1)], fill=(*accent, 20))
            comp = Image.alpha_composite(comp, cb)

            # Left accent strip
            ls = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(ls).rectangle([(x0, y0), (x0 + 5, y1)], fill=(*accent, 255))
            comp = Image.alpha_composite(comp, ls)
            draw = ImageDraw.Draw(comp)

            ix = x0 + 18
            iw = CELL_W - 24
            iy = y0 + 14

            # Number badge circle
            draw.ellipse([(ix, iy), (ix + 26, iy + 26)], fill=(*accent, 200))
            draw.text(
                (ix + 13, iy + 13),
                str(idx + 1),
                font=font_num_b,
                fill=(255, 255, 255, 255),
                anchor="mm",
            )

            # Tip title
            _, t_lines, t_sz = _fit_lines(
                draw, _strip_emojis(tip.title), _FONT_HEADLINE, 22, 16, iw - 32, max_lines=2
            )
            tx = ix + 34
            for line in t_lines:
                draw.text((tx, iy), line, font=font_title, fill=(255, 255, 255, 255))
                iy += int(t_sz * 1.05)
            iy = y0 + 56

            # Tip body
            _, b_lines, b_sz = _fit_lines(
                draw, _strip_emojis(tip.body), _FONT_BODY, 16, 13, iw, max_lines=4
            )
            for line in b_lines:
                draw.text((ix, iy), line, font=font_body, fill=(170, 178, 198, 210))
                iy += int(b_sz * 1.3)

        out = io.BytesIO()
        comp.convert("RGB").save(out, format="PNG")
        return add_brand_overlay(
            out.getvalue(),
            self._cfg.brand_name,
            crop_bars=False,
            corner="bottom_right",
            logo_scale=1.2,
        )

    def _compose_light_magazine_image(self, plan: _TipsPlan) -> bytes:
        """1080×1080 light/white magazine grid (like screenshot 4 style)."""
        from PIL import Image, ImageDraw

        from core.image_utils import _fit_lines, _load_font, add_brand_overlay

        W = H = 1080
        PAD = 32
        HEADER_H = 168
        GAP = 8
        BG = (248, 248, 250)
        DARK = (22, 22, 30)

        comp = Image.new("RGBA", (W, H), (*BG, 255))
        draw = ImageDraw.Draw(comp)

        # ── Header ─────────────────────────────────────────────────────────────
        # Very large bold black title
        font_hl, hl_lines, hl_sz = _fit_lines(
            draw,
            _strip_emojis(plan.topic_title).upper(),
            _FONT_HEADLINE,
            max(68, int(H * 0.066)),
            max(44, int(H * 0.040)),
            W - PAD * 2,
            max_lines=2,
        )
        ty = PAD
        for line in hl_lines:
            draw.text((PAD, ty), line, font=font_hl, fill=(*DARK, 255))
            ty += int(hl_sz * 1.02)

        # Coloured subtitle pill
        accent0 = (0, 102, 204)
        font_sub = _load_font(_FONT_BODY, 22)
        hook_text = _strip_emojis(plan.hook)
        sub_bbox = draw.textbbox((0, 0), hook_text, font=font_sub)
        pill_w = sub_bbox[2] - sub_bbox[0] + 24
        pill_h = sub_bbox[3] - sub_bbox[1] + 12
        pill_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(pill_layer).rounded_rectangle(
            [(PAD, ty + 4), (PAD + pill_w, ty + 4 + pill_h)],
            radius=pill_h // 2,
            fill=(*accent0, 220),
        )
        comp = Image.alpha_composite(comp, pill_layer)
        draw = ImageDraw.Draw(comp)
        draw.text((PAD + 12, ty + 4 + 6), hook_text, font=font_sub, fill=(255, 255, 255, 255))

        # Divider
        dl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(dl).line(
            [(PAD, HEADER_H - 4), (W - PAD, HEADER_H - 4)], fill=(180, 185, 200, 150), width=2
        )
        comp = Image.alpha_composite(comp, dl)
        draw = ImageDraw.Draw(comp)

        # ── 2×5 grid (10 items) ─────────────────────────────────────────────────
        FOOT_H = 55
        GRID_H = H - HEADER_H - FOOT_H
        n_cols, n_rows = 2, 5
        CELL_W = (W - PAD * 2 - GAP) // n_cols
        CELL_H = (GRID_H - GAP * (n_rows - 1)) // n_rows

        num_colors = [
            (0, 102, 204),
            (180, 30, 180),
            (200, 80, 0),
            (0, 150, 80),
            (140, 0, 200),
            (0, 160, 190),
            (210, 130, 0),
            (180, 20, 60),
            (40, 140, 0),
            (100, 60, 200),
        ]

        items = (plan.items + [None] * 10)[:10]
        font_num_b = _load_font(_FONT_HEADLINE, 20)
        font_title = _load_font(_FONT_HEADLINE, 17)
        font_body = _load_font(_FONT_BODY, 13)

        for idx, tip in enumerate(items):
            if tip is None:
                continue
            col = idx % n_cols
            row = idx // n_cols
            nc = num_colors[idx % len(num_colors)]

            x0 = PAD + col * (CELL_W + GAP)
            y0 = HEADER_H + row * (CELL_H + GAP)
            x1 = x0 + CELL_W
            y1 = y0 + CELL_H

            # Very light cell tint + border
            cb = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(cb).rectangle(
                [(x0, y0), (x1, y1)], fill=(*nc, 8), outline=(*nc, 40), width=1
            )
            comp = Image.alpha_composite(comp, cb)
            draw = ImageDraw.Draw(comp)

            ix = x0 + 10
            iw = CELL_W - 20
            iy = y0 + 8

            # Number
            draw.text((ix, iy), str(idx + 1), font=font_num_b, fill=(*nc, 255))
            num_bbox = draw.textbbox((0, 0), str(idx + 1), font=font_num_b)
            tx = ix + num_bbox[2] - num_bbox[0] + 8

            # Title on same line as number
            _, t_lines, t_sz = _fit_lines(
                draw, _strip_emojis(tip.title), _FONT_HEADLINE, 17, 13, iw - tx + ix, max_lines=2
            )
            for i_l, line in enumerate(t_lines):
                draw.text(
                    (tx, iy + i_l * int(t_sz * 1.0)), line, font=font_title, fill=(*DARK, 230)
                )
            iy += max(num_bbox[3] - num_bbox[1], len(t_lines) * int(t_sz * 1.0)) + 5

            # Body text
            _, b_lines, b_sz = _fit_lines(
                draw, _strip_emojis(tip.body), _FONT_BODY, 13, 11, iw, max_lines=3
            )
            for line in b_lines:
                draw.text((ix, iy), line, font=font_body, fill=(90, 95, 110, 220))
                iy += int(b_sz * 1.3)

        # @handle at bottom
        fh2 = _load_font(_FONT_BODY, 20)
        draw.text((PAD, H - 40), "@britetechlifestyle", font=fh2, fill=(110, 115, 130, 200))

        out = io.BytesIO()
        comp.convert("RGB").save(out, format="PNG")
        return add_brand_overlay(
            out.getvalue(),
            self._cfg.brand_name,
            crop_bars=False,
            corner="bottom_right",
            logo_scale=1.2,
        )
