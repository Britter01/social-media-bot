"""Carousel agent — text-based multi-slide post generator for Instagram and Facebook.

Creates a structured **4-slide, text-only** carousel from a topic in two steps:
  1. Claude Sonnet plans the carousel: format, slide headlines and body copy.
  2. Every slide is rendered as a polished dark brand text card with Pillow.

Slide layout (always exactly 4 slides):
  Slide 1 — Cover / hook   (dark text card, no slide number)
  Slide 2 — Value point 1  (dark text card, numbered 01)
  Slide 3 — Value point 2  (dark text card, numbered 02)
  Slide 4 — CTA            (dark text card, no slide number)

Why text-only: AI image generation (Imagen / Higgsfield) was the single biggest
source of failures — quota 429s, hallucinated on-image text, upload errors — and
when it failed the pipeline fell back to single-image "standard" posts. Carousels
here need NO image generation at all: the dark brand cards are pure deterministic
Pillow rendering, so a carousel can never fail to produce its slides. This makes
Instagram/Facebook output reliable and on-brand every time.

Supported formats (the model picks the best fit for the topic):
  tips      — "5 tips for doing X"
  howto     — "Step-by-step: how to X"
  breakdown — "Breaking down X: what it is, why it matters, how to use it"
  compare   — "X vs Y: which is right for you?"
  myths     — "Common myths about X, debunked"

The returned Post has post_type='carousel' and slides=[{headline, body,
image_url, role}].  thumbnail_url is set to the cover slide image so the
dashboard preview works.

Model choice: Sonnet for copy (judgment + brand voice). No image model needed.
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

from core.config import Config, config
from core.models import Post, PostStatus

logger = logging.getLogger(__name__)

_FORMATS = "tips | howto | breakdown | compare | myths"


def _make_scene_cover(carousel_id: str, slide: dict, cfg) -> bytes | None:
    """Attempt to render a lifestyle scene cover; return bytes or None."""
    try:
        from core.cover_image import make_scene_cover

        return make_scene_cover(
            carousel_id,
            slide["headline"],
            slide["body"],
            brand_name=cfg.brand_name,
            brand_tagline=cfg.brand_tagline,
        )
    except Exception:
        logger.warning("Scene cover failed; falling back to dark text card", exc_info=True)
        return None


class CarouselSlide(BaseModel):
    headline: str = Field(description="Bold, punchy slide headline — max 8 words.")
    body: str = Field(description="1-2 supporting sentences. Conversational, not corporate.")


class CarouselPlan(BaseModel):
    format_type: str = Field(description=f"One of: {_FORMATS}")
    cover_headline: str = Field(description="The carousel title/hook — max 10 words.")
    cover_subtext: str = Field(description="One-sentence teaser shown below the cover headline.")
    slides: list[CarouselSlide] = Field(
        description="Exactly 2 value slides. Each delivers one concrete, useful point."
    )
    cta_headline: str = Field(description="Final slide headline — prompt an action or reflection.")
    cta_body: str = Field(description="One sentence CTA. Warm, not pushy.")
    hashtags: list[str] = Field(
        default_factory=list,
        description=(
            "Exactly 5 relevant hashtags WITHOUT the leading '#'. "
            "Choose the strongest mix of broad and niche tags for this topic."
        ),
    )


class CarouselAgent:
    """Generates a 4-slide, text-only carousel post from a regular Post's topic."""

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
                logger.warning("Supabase Storage unavailable for carousel images")

    def create_from_post(self, source: Post, quality_agent=None) -> Post:
        """Generate a carousel Post from an existing post's topic and pillar.

        Returns a new Post (different id) with post_type='carousel' and
        slides populated. The source post is not modified. ``quality_agent``
        is accepted for call-site compatibility but is not used: the slides are
        deterministic brand text cards with no AI photo to QC.
        """
        import uuid as _uuid

        carousel_id = str(_uuid.uuid4())
        plan = self._plan_carousel(source)
        slides_data = self._generate_slides(carousel_id, source, plan)

        carousel = Post(
            id=carousel_id,
            pillar=source.pillar,
            platform=source.platform,
            topic=source.topic,
            caption=self._build_caption(plan),
            hashtags=[h.lstrip("#").strip() for h in plan.hashtags[:5]] or list(source.hashtags),
            title=plan.cover_headline,
            post_type="carousel",
            slides=slides_data,
            thumbnail_url=slides_data[0]["image_url"] if slides_data else source.thumbnail_url,
        )
        carousel.mark(PostStatus.MEDIA_READY)
        logger.info(
            "Created text carousel post %s (%s/%s) with %d slides",
            carousel.id,
            carousel.pillar,
            carousel.platform,
            len(slides_data),
        )
        return carousel

    # --- Internals -----------------------------------------------------------

    def _plan_carousel(self, post: Post) -> CarouselPlan:
        """Ask Claude to plan the 4-slide carousel format and slide copy."""
        import json

        # Derive the best available topic signal — topic field is canonical, but
        # old or partially-built posts may have it empty; fall back to title then
        # the first sentence of caption so we always have something to plan from.
        topic = (post.topic or "").strip()
        if not topic:
            topic = (post.title or "").strip()
        if not topic and post.caption:
            topic = post.caption.split("\n")[0][:150].strip()
        if not topic:
            raise RuntimeError(
                "Cannot plan carousel: post has no topic, title, or caption to work from"
            )

        system = (
            f"You are the content strategist for {self._cfg.brand_name}, "
            f"founded by {self._cfg.brand_founder}. "
            f'Tagline: "{self._cfg.brand_tagline}". '
            "Voice: clear, confident, warm. Never patronising. Short sentences. "
            "You are planning a swipeable carousel of exactly 4 text slides: "
            "a cover hook, two value slides, and a closing call-to-action. "
            "Every slide must earn its swipe — concrete value, no filler. "
            "Make the cover impossible to scroll past. "
            "Respond with a valid JSON object ONLY — no markdown, no explanation."
        )
        prompt = (
            f"Plan a 4-slide carousel for the '{post.pillar}' pillar on {post.platform.upper()}.\n"
            f"Topic: {topic}\n\n"
            f"Choose the best format for this topic ({_FORMATS}). "
            "Return EXACTLY 2 value slides (the cover and CTA are separate fields). "
            "Each value slide needs a punchy headline and 1-2 sentences of useful copy. "
            "These render as text cards — no images — so make the words carry the value.\n\n"
            "Return a JSON object with exactly these fields:\n"
            '{"format_type": "one of: tips|howto|breakdown|compare|myths", '
            '"cover_headline": "max 10 words", '
            '"cover_subtext": "one-sentence teaser", '
            '"slides": [{"headline": "max 8 words", "body": "1-2 sentences"}, '
            '{"headline": "max 8 words", "body": "1-2 sentences"}], '
            '"cta_headline": "action or reflection prompt", '
            '"cta_body": "one warm sentence", '
            '"hashtags": ["tag1", "tag2", "tag3", "tag4", "tag5"]}'
            "\nProvide exactly 5 hashtags WITHOUT the # prefix — strongest mix of broad and niche."
        )
        response = self._client.messages.create(
            model=self._cfg.model_creative,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if Claude wraps the JSON
        if "```json" in text:
            text = text.split("```json", 1)[1].rsplit("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].rsplit("```", 1)[0].strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Claude returned invalid JSON: {exc}\nRaw response: {text[:300]}"
            ) from exc
        # Ensure hashtags key exists in case an older model response omits it
        if "hashtags" not in data:
            data["hashtags"] = []
        plan = CarouselPlan.model_validate(data)
        # Enforce exactly 2 value slides so we always produce a 4-slide carousel.
        if len(plan.slides) > 2:
            plan.slides = plan.slides[:2]
        elif len(plan.slides) < 2:
            raise RuntimeError(f"Carousel plan returned {len(plan.slides)} value slides, need 2")
        logger.info(
            "Planned text carousel '%s' (%s, 4 slides)",
            plan.cover_headline,
            plan.format_type,
        )
        return plan

    def _generate_slides(self, carousel_id: str, post: Post, plan: CarouselPlan) -> list[dict]:
        """Render the 4 text slides and return slide dicts ready for the Post model.

        Layout:
          index 0 — cover hook   (no slide number)
          index 1 — value 01     (numbered 01)
          index 2 — value 02     (numbered 02)
          index 3 — CTA          (no slide number)

        Carousels alternate between dark and light brand themes so the feed
        has visual variety. The theme is chosen deterministically from
        carousel_id so the same post always produces the same look.
        """
        from core.image_utils import add_brand_overlay, make_dark_text_card

        # Alternate dark / light per carousel (50 / 50 split, deterministic).
        card_theme = "light" if int(carousel_id.replace("-", "")[:8], 16) % 2 == 1 else "dark"

        # Flat list: cover → value 1 → value 2 → cta
        all_slides = [
            {"headline": plan.cover_headline, "body": plan.cover_subtext, "role": "cover"},
            {"headline": plan.slides[0].headline, "body": plan.slides[0].body, "role": "content"},
            {"headline": plan.slides[1].headline, "body": plan.slides[1].body, "role": "content"},
            {"headline": plan.cta_headline, "body": plan.cta_body, "role": "cta"},
        ]

        result: list[dict] = []
        content_count = 0
        for i, slide in enumerate(all_slides):
            try:
                role = slide["role"]
                if role == "content":
                    content_count += 1
                    slide_number = content_count  # 01, 02
                else:
                    slide_number = None  # cover and CTA show no number

                if i == 0:
                    # Slide 0: try a lifestyle scene cover (text warped onto a laptop
                    # screen mockup). Falls back to themed text card if scenes are
                    # missing or screen detection fails.
                    card = _make_scene_cover(carousel_id, slide, self._cfg)
                    if card is None:
                        card = make_dark_text_card(
                            slide["headline"],
                            slide["body"],
                            slide_number,
                            brand_name=self._cfg.brand_name,
                            brand_tagline=self._cfg.brand_tagline,
                            theme=card_theme,
                        )
                    # Auto-pick quietest corner for the logo on a photo cover
                    logo_corner: str | None = None
                    logo_scale = 1.0
                else:
                    card = make_dark_text_card(
                        slide["headline"],
                        slide["body"],
                        slide_number,
                        brand_name=self._cfg.brand_name,
                        brand_tagline=self._cfg.brand_tagline,
                        theme=card_theme,
                    )
                    logo_corner = "top_right"
                    logo_scale = 1.625

                # Brand logo overlay.
                # crop_bars=False: text cards have uniform dark margins that the
                # hallucinated-bar cropper would wrongly strip (clipping the tagline).
                # Scene covers are already square-cropped so bars cannot appear either.
                final_bytes = add_brand_overlay(
                    card,
                    self._cfg.brand_name,
                    self._cfg.brand_tagline,
                    corner=logo_corner,
                    crop_bars=False,
                    logo_scale=logo_scale,
                )

                image_url = self._upload(carousel_id, i, final_bytes)
                result.append(
                    {
                        "headline": slide["headline"][:120],
                        "body": slide["body"][:400],
                        "image_url": image_url,
                        "role": role,
                    }
                )
                logger.debug("Rendered text slide %d (%s) for post %s", i, role, post.id)
            except Exception:
                logger.exception("Failed rendering slide %d of post %s", i, post.id)

        if not result:
            logger.error(
                "All %d slides failed to render for post %s — carousel will be empty",
                len(all_slides),
                post.id,
            )
        elif len(result) < len(all_slides):
            logger.warning(
                "%d/%d slides rendered for post %s — carousel may be incomplete",
                len(result),
                len(all_slides),
                post.id,
            )
        return result

    def _upload(self, post_id: str, slide_index: int, image_bytes: bytes) -> str:
        """Upload slide image to Supabase; return public URL."""
        if self._storage is None:
            raise RuntimeError(
                "Supabase Storage not configured — check SUPABASE_URL, SUPABASE_KEY, "
                "and that the 'media' bucket exists in Supabase Storage"
            )
        path = f"carousels/{post_id}/slide_{slide_index:02d}.png"
        return self._storage.upload(path, image_bytes, content_type="image/png")

    @staticmethod
    def _build_caption(plan: CarouselPlan) -> str:
        """Assemble a caption that works as a standalone Instagram/Facebook post."""
        lines = [plan.cover_headline, "", plan.cover_subtext, ""]
        for i, slide in enumerate(plan.slides, 1):
            lines.append(f"{i}. {slide.headline}")
        lines += ["", plan.cta_body]
        return "\n".join(lines)
