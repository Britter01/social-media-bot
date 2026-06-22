"""Daily AI News Carousel agent.

Fetches today's top 3 AI news stories via web search, selects and summarises
them with Claude, and renders a 5-slide branded carousel:

  Slide 1 — Cover: "Today in AI" + date
  Slide 2 — Story 1 (headline + summary + why it matters)
  Slide 3 — Story 2
  Slide 4 — Story 3
  Slide 5 — CTA: follow for daily AI briefings

Uses the same dark text-card rendering as the carousel agent so the visual
style is consistent with all other Brite Tech Lifestyle carousel posts.
"""

from __future__ import annotations

import logging
import uuid as _uuid

import anthropic
from pydantic import BaseModel, Field

from core.config import Config, config
from core.models import Platform, Post, PostStatus

logger = logging.getLogger(__name__)

_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 8,
}
_MAX_WEB_TURNS = 5

# Brite Blue abstract background for the news carousel — same rich AI aesthetic
# as the infographics, kept in the brand's accent blue. Generated once via
# Higgsfield (Imagen fallback), then stored as a reusable template.
_NEWS_BG_PROMPT = (
    "abstract AI neural network background, deep Brite Blue and electric cobalt tones, "
    "glowing interconnected nodes and luminous data trails, dark navy cosmic depth, "
    "subtle geometric grid lines, cinematic volumetric light, rich saturated blue gradient, "
    "premium editorial tech aesthetic, 3D depth, no text, no letters, no words"
)


class NewsStory(BaseModel):
    headline: str = Field(description="News headline — max 10 words, factual, no hype.")
    summary: str = Field(
        description="What happened — 1-2 factual sentences. Include key names and figures."
    )  # noqa: E501
    insight: str = Field(
        description="Why it matters to everyday tech users — 1 sentence, max 12 words."
    )  # noqa: E501


class NewsCarouselPlan(BaseModel):
    lead_headline: str = Field(
        description="Cover slide headline — e.g. 'Today in AI'. Max 6 words, punchy."
    )
    caption: str = Field(
        description=(
            "Instagram/Facebook caption — 2-3 warm sentences summarising today's AI highlights. "
            "End with a light engagement question. No hashtags here."
        )
    )
    hashtags: list[str] = Field(
        description=(
            "Exactly 5 relevant hashtags WITHOUT the # prefix. "
            "Mix: AINews, ArtificialIntelligence + 3 topic-specific niche tags."
        )
    )
    stories: list[NewsStory] = Field(
        description=(
            "Exactly 3 top AI news stories from today's research, ordered by importance. "
            "All three MUST be populated — never leave any story out."
        )
    )


class NewsAgent:
    """Fetches today's top AI news and produces a branded daily news carousel."""

    def __init__(self, cfg: Config = config) -> None:
        cfg.require("anthropic_api_key")
        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

        self._storage = None
        if cfg.supabase_url and cfg.supabase_key:
            try:
                from core.storage import get_storage

                self._storage = get_storage(cfg)
            except Exception:
                logger.warning("NewsAgent: Supabase Storage unavailable", exc_info=True)

    _BG_TEMPLATE_PATH = "templates/news_carousel_bg.png"

    def _get_bg_template(self) -> bytes | None:
        """Return the news-carousel background template bytes.

        First run: generate a rich Brite Blue abstract background via Higgsfield
        (Imagen fallback), bake in the readability gradient, and upload to
        Supabase. Every subsequent run is a fast download of that template.
        If no image API is available, falls back to a pure Pillow gradient.
        Returns None only when storage is unavailable AND no fallback renders.
        """
        from core.image_utils import make_news_bg_template

        # Persistent template hit — reuse the stored background, no generation.
        if self._storage is not None:
            try:
                cached = self._storage.download(self._BG_TEMPLATE_PATH)
                if cached:
                    logger.info("NewsAgent: using stored background template")
                    return cached
            except Exception:
                logger.warning("NewsAgent: template download failed", exc_info=True)

        # First run (or storage miss) — generate the AI base, bake the template.
        logger.info("NewsAgent: generating background template for the first time")
        ai_base = self._generate_ai_background()
        try:
            template = make_news_bg_template(base_bytes=ai_base)
        except Exception:
            logger.exception("NewsAgent: failed to bake background template")
            return None

        if self._storage is not None:
            try:
                self._storage.upload(self._BG_TEMPLATE_PATH, template, content_type="image/png")
                logger.info("NewsAgent: background template stored at %s", self._BG_TEMPLATE_PATH)
            except Exception:
                logger.warning("NewsAgent: failed to store background template", exc_info=True)

        return template

    def _generate_ai_background(self) -> bytes | None:
        """Generate a Brite Blue abstract background via Higgsfield (Imagen fallback).

        Returns the raw image bytes, or None if no image API is configured or
        both providers fail — in which case the caller bakes a pure gradient.
        """
        if not (self._cfg.higgsfield_api_key or self._cfg.google_api_key):
            logger.info("NewsAgent: no image API configured — using gradient background")
            return None

        try:
            from agents.infographic_agent import InfographicAgent

            ia = InfographicAgent(self._cfg)
        except Exception:
            logger.warning("NewsAgent: could not init image agent for background", exc_info=True)
            return None

        if self._cfg.higgsfield_api_key:
            try:
                logger.info("NewsAgent: generating background via Higgsfield")
                return ia._higgsfield_background(aspect_ratio="1:1", prompt=_NEWS_BG_PROMPT)
            except Exception:
                logger.warning(
                    "NewsAgent: Higgsfield background failed; trying Imagen", exc_info=True
                )

        if self._cfg.google_api_key:
            try:
                logger.info("NewsAgent: generating background via Imagen")
                return ia._imagen_background(aspect_ratio="1:1", prompt=_NEWS_BG_PROMPT)
            except Exception:
                logger.warning("NewsAgent: Imagen background failed", exc_info=True)

        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def create_news_carousel(self, platforms: list[str] | None = None) -> list[Post]:
        """Fetch today's top AI news and return carousel Posts ready for scheduling."""
        if platforms is None:
            configured = set(self._cfg.configured_platforms())
            platforms = [
                p for p in [Platform.INSTAGRAM.value, Platform.FACEBOOK.value] if p in configured
            ] or [Platform.INSTAGRAM.value]

        logger.info("NewsAgent: fetching today's AI news via web search")
        research = self._fetch_ai_news()

        logger.info("NewsAgent: planning news carousel")
        plan = self._plan_news(research)
        logger.info(
            "NewsAgent: planned '%s' with %d stories", plan.lead_headline, len(plan.stories)
        )

        carousel_id = str(_uuid.uuid4())
        slides_data = self._render_slides(carousel_id, plan)
        if not slides_data:
            raise RuntimeError("NewsAgent: no slides were rendered")

        caption = self._build_caption(plan)
        hashtags = [h.lstrip("#").strip() for h in plan.hashtags[:5]]

        posts: list[Post] = []
        for plat in platforms:
            post = Post(
                id=str(_uuid.uuid4()),
                pillar="AI News",
                platform=plat,
                topic="AI News",
                title=plan.lead_headline,
                caption=caption,
                hashtags=hashtags,
                post_type="carousel",
                slides=slides_data,
                thumbnail_url=slides_data[0]["image_url"] if slides_data else "",
                status=PostStatus.MEDIA_READY.value,
            )
            posts.append(post)
            logger.info("NewsAgent: created %s news carousel %s", plat, post.id)

        return posts

    # ── Internals ─────────────────────────────────────────────────────────────

    def _fetch_ai_news(self) -> str:
        """Fetch today's top AI news stories via multi-turn web search."""
        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%A, %B %d, %Y")
        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"Today is {today}. Search for the 3 most important AI news stories "
                    "published in the last 24 hours. Look for: major model releases, "
                    "company announcements, research breakthroughs, regulatory news, "
                    "or significant funding rounds. "
                    "For each story report: what happened, who was involved, and specific "
                    "facts (numbers, dates, names, quotes). Prioritise novelty and impact."
                ),
            }
        ]

        response = None
        for _ in range(_MAX_WEB_TURNS):
            response = self._client.messages.create(
                model=self._cfg.model_creative,
                max_tokens=3000,
                tools=[_WEB_SEARCH_TOOL],
                messages=messages,
            )
            if response.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": response.content})

        if response is None:
            raise RuntimeError("NewsAgent: web search produced no response")

        return "\n".join(getattr(b, "text", "") for b in response.content if hasattr(b, "text"))

    def _plan_news(self, raw_research: str) -> NewsCarouselPlan:
        """Ask Claude to extract 3 stories and build the carousel plan."""
        plan_tool = {
            "name": "create_news_carousel_plan",
            "description": "Create a daily AI news carousel plan from web research.",
            "input_schema": NewsCarouselPlan.model_json_schema(),
        }
        response = self._client.messages.create(
            model=self._cfg.model_creative,
            max_tokens=2000,
            system=(
                f"You are the content editor for {self._cfg.brand_name} — "
                f'"{self._cfg.brand_tagline}". '
                "Voice: clear, confident, warm. Short sentences. Never patronising. "
                "You are creating a daily AI news carousel for Instagram and Facebook. "
                "Select the 3 most important and interesting AI stories from the research. "
                "Be specific — include company names, numbers, and dates. "
                "Each story insight must explain WHY it matters to everyday tech users "
                "in plain language. No jargon. No hype. Make it feel like a trusted friend "
                "sharing the day's news over coffee."
            ),
            tools=[plan_tool],
            tool_choice={"type": "tool", "name": "create_news_carousel_plan"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Research:\n{raw_research}\n\n"
                        "Select the 3 most important AI stories from today and create "
                        "the news carousel plan. All 3 stories are required — never leave "
                        "any field empty."
                    ),
                }
            ],
        )
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and block.name == "create_news_carousel_plan"
            ):
                return NewsCarouselPlan(**block.input)
        raise RuntimeError("NewsAgent: news carousel plan tool call returned no result")

    def _render_slides(self, carousel_id: str, plan: NewsCarouselPlan) -> list[dict]:
        """Render 5 branded text cards and upload each to Supabase storage."""
        import re
        from datetime import UTC, datetime

        from core.image_utils import add_brand_overlay, make_dark_text_card

        def _clean(text: str) -> str:
            """Strip emoji and non-BMP characters the brand font cannot render."""
            return re.sub(r"[^\x00-\xFF -⁯℀-⅏]", "", text).strip()

        today = datetime.now(UTC).strftime("%d %b %Y")

        all_slides = [
            {
                "headline": _clean(plan.lead_headline).upper(),
                "body": f"{today}  ·  3 stories shaping AI today",
                "role": "cover",
                "slide_number": None,
            },
        ]
        for i, story in enumerate(plan.stories[:3], 1):
            all_slides.append(
                {
                    "headline": _clean(story.headline),
                    "body": _clean(f"{story.summary}\n\nWhy it matters: {story.insight}"),
                    "role": "content",
                    "slide_number": i,
                }
            )
        all_slides.append(
            {
                "headline": "Follow for Daily AI News",
                "body": (
                    f"Get your daily AI briefing from {self._cfg.brand_name}. "
                    "Follow to stay ahead of the curve."
                ),
                "role": "cta",
                "slide_number": None,
            }
        )

        # Fetch (or generate on first run) the shared gradient background template.
        bg_bytes = self._get_bg_template()

        result: list[dict] = []
        for idx, slide in enumerate(all_slides):
            try:
                image_bytes = make_dark_text_card(
                    headline=slide["headline"],
                    body=slide["body"],
                    slide_number=slide["slide_number"],
                    brand_name=self._cfg.brand_name,
                    brand_tagline=self._cfg.brand_tagline,
                    theme="blue",
                    bg_bytes=bg_bytes,
                )
                image_bytes = add_brand_overlay(
                    image_bytes,
                    self._cfg.brand_name,
                    self._cfg.brand_tagline,
                    corner="top_right",
                    crop_bars=False,
                )
                url = self._upload(carousel_id, idx, image_bytes)
                result.append(
                    {
                        "headline": slide["headline"],
                        "body": slide["body"],
                        "role": slide["role"],
                        "image_url": url,
                    }
                )
            except Exception:
                logger.exception("NewsAgent: failed to render/upload slide %d", idx)

        return result

    def _upload(self, carousel_id: str, slide_index: int, image_bytes: bytes) -> str:
        if self._storage is None:
            raise RuntimeError(
                "Supabase Storage not configured — check SUPABASE_URL and SUPABASE_KEY"
            )
        path = f"carousels/{carousel_id}/slide_{slide_index:02d}.png"
        return self._storage.upload(path, image_bytes, content_type="image/png")

    @staticmethod
    def _build_caption(plan: NewsCarouselPlan) -> str:
        return plan.caption
