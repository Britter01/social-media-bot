"""Carousel agent — multi-slide post generator for Instagram and Facebook.

Creates a structured carousel post from a topic in two steps:
  1. Claude Sonnet plans the carousel: format, slide headlines and body copy.
  2. Slides are rendered using a mix of Imagen photos and pure dark brand
     text cards, assembled with Pillow.

Slide design:
  Cover (slide 0)  — Imagen photo + gradient + bold headline (make_cover_card)
  Content slides   — alternate between:
                       dark brand text card (odd-numbered content: 01, 03 …)
                       Imagen photo + text strip (even-numbered content: 02, 04 …)
  CTA (last slide) — dark brand text card (no Imagen)

This alternating pattern creates visual rhythm while keeping Imagen calls to
a minimum (~half the slides), reduces the risk of AI-hallucinated text inside
photos, and produces a clean, premium-editorial look consistent with top
Instagram carousels.

Supported formats (the model picks the best fit for the topic):
  tips      — "5 tips for doing X"
  howto     — "Step-by-step: how to X"
  breakdown — "Breaking down X: what it is, why it matters, how to use it"
  compare   — "X vs Y: which is right for you?"
  myths     — "Common myths about X, debunked"

The returned Post has post_type='carousel' and slides=[{headline, body,
image_url, role}].  thumbnail_url is set to the cover slide image so the
dashboard preview works.

Model choice: Sonnet for copy (judgment + brand voice); Imagen Standard for
photos (better instruction-following than Fast, especially the no-text rule).
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

from core.config import Config, config
from core.models import Post, PostStatus

logger = logging.getLogger(__name__)

_FORMATS = "tips | howto | breakdown | compare | myths"

_ASPECT_RATIO = {
    "instagram": "1:1",
    "facebook": "1:1",
    "twitter": "16:9",
    "linkedin": "1:1",
}

# Imagen prompt prefix — purely visual, no topic text that Imagen might render as letters
_IMG_PREFIX = (
    "Editorial lifestyle photograph. "
    "Clean minimal composition, soft natural light, modern technology in a beautifully "
    "lived-in space, warm and confident mood, high detail. "
    "ABSOLUTE RULE — zero text of any kind: no letters, words, numbers, titles, "
    "labels, signs, watermarks, logos, or any readable characters anywhere — "
    "not even partially visible, reflected, or blurred. "
    "Device screens show only abstract bokeh or ambient glow, never UI or text. "
    "Do not depict any written language anywhere. "
)


class CarouselSlide(BaseModel):
    headline: str = Field(description="Bold, punchy slide headline — max 8 words.")
    body: str = Field(description="1-2 supporting sentences. Conversational, not corporate.")
    image_prompt: str = Field(
        description=(
            "Visual scene for slides that use a photo background — describe the setting, "
            "mood, and subject using only spatial and sensory language. "
            "No text, logos, or overlay graphics. "
            "This will be prefixed with a strict no-text instruction."
        )
    )


class CarouselPlan(BaseModel):
    format_type: str = Field(description=f"One of: {_FORMATS}")
    cover_headline: str = Field(description="The carousel title/hook — max 10 words.")
    cover_subtext: str = Field(description="One-sentence teaser shown below the cover headline.")
    cover_image_prompt: str = Field(description="Visual direction for the cover slide photo.")
    slides: list[CarouselSlide] = Field(
        description="4–6 content slides. Each builds on the cover hook."
    )
    cta_headline: str = Field(description="Final slide headline — prompt an action or reflection.")
    cta_body: str = Field(description="One sentence CTA. Warm, not pushy.")


class CarouselAgent:
    """Generates a multi-slide carousel post from a regular Post's topic."""

    def __init__(self, cfg: Config = config) -> None:
        cfg.require("anthropic_api_key", "google_api_key")
        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

        from google import genai

        self._genai_client = genai.Client(api_key=cfg.google_api_key)
        self._genai = genai

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
        slides populated. The source post is not modified.
        Pass ``quality_agent`` to run per-slide QC before uploading.
        """
        import uuid as _uuid

        carousel_id = str(_uuid.uuid4())
        plan = self._plan_carousel(source)
        slides_data = self._generate_slides(carousel_id, source, plan, quality_agent=quality_agent)

        carousel = Post(
            id=carousel_id,
            pillar=source.pillar,
            platform=source.platform,
            topic=source.topic,
            caption=self._build_caption(plan),
            hashtags=list(source.hashtags),
            title=plan.cover_headline,
            post_type="carousel",
            slides=slides_data,
            thumbnail_url=slides_data[0]["image_url"] if slides_data else source.thumbnail_url,
        )
        carousel.mark(PostStatus.MEDIA_READY)
        logger.info(
            "Created carousel post %s (%s/%s) with %d slides",
            carousel.id,
            carousel.pillar,
            carousel.platform,
            len(slides_data),
        )
        return carousel

    # --- Internals -----------------------------------------------------------

    def _plan_carousel(self, post: Post) -> CarouselPlan:
        """Ask Claude to plan the carousel format and slide copy."""
        system = (
            f"You are the content strategist for {self._cfg.brand_name}, "
            f"founded by {self._cfg.brand_founder}. "
            f'Tagline: "{self._cfg.brand_tagline}". '
            "Voice: clear, confident, warm. Never patronising. Short sentences. "
            "You are planning a carousel post — a swipeable series of slides. "
            "Each slide should give genuine value. Avoid filler. "
            "Make the cover slide impossible to scroll past."
        )
        prompt = (
            f"Plan a carousel post for the '{post.pillar}' pillar on {post.platform.upper()}.\n"
            f"Topic: {post.topic}\n\n"
            f"Choose the best format for this topic ({_FORMATS}). "
            "Return 4-6 content slides (not counting cover and CTA). "
            "Each slide needs a headline, body copy, and an image direction — "
            "describe a real-world scene or visual metaphor that suits the slide, "
            "no text overlays, no infographic styles, no logos or brand names in the scene. "
            "Note: most content slides will be rendered as dark text cards (no photo), "
            "so image_prompt is only used for alternating photo slides — keep descriptions "
            "atmospheric and spatial rather than concept-driven."
        )
        response = self._client.messages.parse(
            model=self._cfg.model_creative,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=CarouselPlan,
        )
        plan = response.parsed_output
        if plan is None:
            raise RuntimeError("Claude returned no carousel plan")
        logger.info(
            "Planned carousel '%s' (%s, %d slides)",
            plan.cover_headline,
            plan.format_type,
            len(plan.slides),
        )
        return plan

    def _generate_slides(
        self, carousel_id: str, post: Post, plan: CarouselPlan, quality_agent=None
    ) -> list[dict]:
        """Render all slides and return slide dicts ready for the Post model.

        Slide layout:
          Index 0             — cover photo (Imagen + make_cover_card)
          Content odd (1,3…)  — dark text card (make_dark_text_card, no Imagen)
          Content even (2,4…) — photo text card (Imagen + make_photo_text_card)
          Last index          — CTA dark card (make_dark_text_card, no Imagen)
        """
        from google.genai import types

        from core.image_utils import (
            add_brand_overlay,
            make_cover_card,
            make_dark_text_card,
            make_photo_text_card,
        )

        aspect_ratio = _ASPECT_RATIO.get(post.platform, "1:1")

        # Flat list: cover → content… → cta
        all_slides = [
            {
                "headline": plan.cover_headline,
                "body": plan.cover_subtext,
                "image_prompt": _IMG_PREFIX + plan.cover_image_prompt,
                "role": "cover",
            }
        ]
        for s in plan.slides:
            all_slides.append(
                {
                    "headline": s.headline,
                    "body": s.body,
                    "image_prompt": _IMG_PREFIX + s.image_prompt,
                    "role": "content",
                }
            )
        all_slides.append(
            {
                "headline": plan.cta_headline,
                "body": plan.cta_body,
                "image_prompt": "",  # CTA never uses Imagen
                "role": "cta",
            }
        )

        result: list[dict] = []
        content_count = 0  # 1-based counter for numbered slides

        for i, slide in enumerate(all_slides):
            role = slide["role"]
            try:
                if role == "content":
                    content_count += 1
                    slide_number = content_count
                    use_photo = content_count % 2 == 0  # 01=dark, 02=photo, 03=dark …
                elif role == "cover":
                    slide_number = None
                    use_photo = True
                else:  # cta
                    slide_number = None
                    use_photo = False

                # Generate Imagen photo where needed
                raw_bytes: bytes | None = None
                if use_photo:
                    try:
                        response = self._genai_client.models.generate_images(
                            model=self._cfg.imagen_model,
                            prompt=slide["image_prompt"],
                            config=types.GenerateImagesConfig(
                                number_of_images=1,
                                aspect_ratio=aspect_ratio,
                            ),
                        )
                        images = getattr(response, "generated_images", None) or []
                        if images:
                            raw_bytes = images[0].image.image_bytes
                        else:
                            logger.warning(
                                "Imagen returned no image for slide %d; using dark card", i
                            )
                            use_photo = False
                    except Exception:
                        logger.exception("Imagen failed for slide %d; using dark card", i)
                        use_photo = False

                # Render the slide
                if role == "cover" and raw_bytes:
                    card = make_cover_card(raw_bytes, slide["headline"], slide["body"])
                elif use_photo and raw_bytes:
                    card = make_photo_text_card(
                        raw_bytes, slide["headline"], slide["body"], slide_number
                    )
                else:
                    # Dark text card — no Imagen needed
                    card = make_dark_text_card(
                        slide["headline"],
                        slide["body"],
                        slide_number,
                        brand_name=self._cfg.brand_name,
                        brand_tagline=self._cfg.brand_tagline,
                    )

                # Brand logo overlay on every slide
                final_bytes = add_brand_overlay(card, self._cfg.brand_name, self._cfg.brand_tagline)

                # QC check
                if quality_agent is not None:
                    from agents.quality_agent import QualityError

                    try:
                        quality_agent.check_image_bytes(post, final_bytes, label=f"slide {i}")
                    except QualityError as exc:
                        logger.warning("QC: slide %d failed (%s) — skipping", i, exc)
                        continue

                image_url = self._upload(carousel_id, i, final_bytes)
                result.append(
                    {
                        "headline": slide["headline"][:120],
                        "body": slide["body"][:400],
                        "image_url": image_url,
                        "role": role,
                    }
                )
                logger.debug("Rendered carousel slide %d (%s) for post %s", i, role, post.id)

            except Exception:
                logger.exception("Failed rendering slide %d of post %s", i, post.id)

        return result

    def _upload(self, post_id: str, slide_index: int, image_bytes: bytes) -> str:
        """Upload slide image to Supabase; return public URL or local path."""
        path = f"carousels/{post_id}/slide_{slide_index:02d}.png"
        if self._storage is not None:
            return self._storage.upload(path, image_bytes, content_type="image/png")
        import os
        import tempfile

        local_path = os.path.join(tempfile.gettempdir(), path.replace("/", "_"))
        with open(local_path, "wb") as fh:
            fh.write(image_bytes)
        return local_path

    @staticmethod
    def _build_caption(plan: CarouselPlan) -> str:
        """Assemble a caption that works as a standalone Instagram/Facebook post."""
        lines = [plan.cover_headline, "", plan.cover_subtext, ""]
        for i, slide in enumerate(plan.slides, 1):
            lines.append(f"{i}. {slide.headline}")
        lines += ["", plan.cta_body]
        return "\n".join(lines)
