"""Research agent — finds trending topics and feeds the best into content.

The pipeline upstream of everything else. In two Claude API calls it:

  1. **Discovers** what's trending across the brand's themes (AI,
     productivity, fitness tech, tech lifestyle, reviews) using the
     server-side ``web_search`` tool, so findings are grounded in
     current, real sources rather than the model's training data.
  2. **Scores and decides** — a second call uses structured outputs
     (``messages.parse``) to rate each topic for fit with the Brite Tech
     Lifestyle brand, map it onto a content pillar, and pick the platform
     and angle that suit it.

The two-call split is deliberate: the search call returns cited prose
(web search emits citations, which are incompatible with structured
outputs in a single request), and the scoring call turns that prose into
clean, validated ``Topic`` objects. The large brand brief is sent as a
prompt-cached system block, so both calls — and every run — reuse it at
~0.1x cost after the first.

The best-scoring topics are persisted to the Supabase ``topics`` table
and handed to the content agent, which turns each into a draft post.
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

from core.config import Config, config
from core.models import Pillar, Platform, Post, Topic, TopicStatus

logger = logging.getLogger(__name__)

# Server-side web search tool. The 2026-02-09 version has dynamic result
# filtering built in (Claude filters results before they hit the context),
# which improves accuracy and token efficiency on Opus 4.8.
_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}

# Web search runs a server-side loop; if it hits the iteration cap it
# returns stop_reason="pause_turn" and we re-send to let it resume.
_MAX_CONTINUATIONS = 5

_VALID_PILLARS = {p.value for p in Pillar}
_VALID_PLATFORMS = {p.value for p in Platform}


class ScoredTopic(BaseModel):
    """One trending topic, scored and assigned by Claude."""

    title: str = Field(description="A short, specific topic title.")
    summary: str = Field(description="1-2 sentences on what's happening and why it's trending.")
    pillar: str = Field(
        description=(
            "The single best-fit content pillar, EXACTLY one of: "
            "'AI Guide', 'Tech Lifestyle', 'Productivity', 'Fitness Tech', 'Review'."
        )
    )
    platform: str = Field(
        description=(
            "The best platform for this topic, EXACTLY one of: "
            "'instagram', 'twitter', 'linkedin', 'youtube', 'tiktok'."
        )
    )
    content_angle: str = Field(
        description="A concrete angle/hook for the post — what to actually make."
    )
    relevance_score: int = Field(
        description="Brand-fit score from 0 (off-brand) to 100 (perfect fit)."
    )
    rationale: str = Field(description="One sentence justifying the score and choices.")
    sources: list[str] = Field(
        default_factory=list, description="Supporting source URLs from the research, if any."
    )


class TopicSlate(BaseModel):
    """The full set of scored topics Claude returns."""

    topics: list[ScoredTopic]


def _system_prompt(brand_name: str, founder: str, tagline: str) -> str:
    """The large, stable brand brief that gets prompt-cached."""
    pillars = (
        "AI Guide — practical, accessible explainers that make AI useful today.\n"
        "Tech Lifestyle — how good technology fits into a well-lived life.\n"
        "Productivity — tools and systems that give people their time back.\n"
        "Fitness Tech — wearables, apps, and gear for a healthier life.\n"
        "Review — honest, hands-on verdicts. Pros, cons, who it's for."
    )
    return (
        f"You are the content strategist for {brand_name}, founded by {founder}.\n"
        f'Tagline: "{tagline}"\n\n'
        "Your job is to find trending topics worth posting about and judge how "
        "well each fits the brand. Score for relevance to the audience: people "
        "who love technology and want it to genuinely improve their lives — no "
        "hype-chasers, no fake news, no clickbait.\n\n"
        "CONTENT PILLARS:\n"
        f"{pillars}\n\n"
        "PLATFORM FIT: instagram (visual, lifestyle), twitter (fast takes, news), "
        "linkedin (professional insight), youtube (explainers, reviews), "
        "tiktok (short, energetic, native video).\n\n"
        "Favour topics that are timely, specific, and let the brand add real value. "
        "Penalise vague, off-brand, or purely sensational topics. Never invent "
        "products, specs, or events."
    )


class ResearchAgent:
    """Discovers trending topics, scores them, and seeds the content pipeline."""

    def __init__(
        self,
        cfg: Config = config,
        content_agent=None,
        db=None,
    ) -> None:
        """Build the agent.

        ``content_agent`` and ``db`` are injected so the agent can run
        end-to-end (discover → persist → generate content) but stay easy
        to unit test. Both are optional: with neither, ``discover`` still
        works and returns scored topics in memory.
        """
        cfg.require("anthropic_api_key")
        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self._content_agent = content_agent
        self._db = db
        self._system = _system_prompt(cfg.brand_name, cfg.brand_founder, cfg.brand_tagline)

    # --- Public API ------------------------------------------------------

    def discover(self, categories: list[str] | None = None) -> list[Topic]:
        """Find trending topics and return them scored and assigned.

        Raises on hard API failures so the caller can decide what to do.
        """
        categories = categories or self._cfg.research_categories
        findings = self._search_trends(categories)
        if not findings.strip():
            logger.warning("Web search returned no findings; no topics produced")
            return []
        return self._score_and_decide(findings, categories)

    def select_best(self, topics: list[Topic], limit: int | None = None) -> list[Topic]:
        """Return the highest-scoring topics that clear the relevance bar.

        Topics below ``min_topic_relevance`` are marked rejected; the rest
        are sorted by score and capped at ``topics_per_run`` (or ``limit``).
        """
        threshold = self._cfg.min_topic_relevance
        cap = limit if limit is not None else self._cfg.topics_per_run

        eligible = []
        for topic in topics:
            if topic.relevance_score >= threshold:
                eligible.append(topic)
            else:
                topic.mark(TopicStatus.REJECTED)

        eligible.sort(key=lambda t: t.relevance_score, reverse=True)
        chosen = eligible[:cap]
        for topic in chosen:
            topic.mark(TopicStatus.SELECTED)
        return chosen

    def run(
        self,
        categories: list[str] | None = None,
        *,
        persist: bool = True,
        generate_content: bool = True,
    ) -> list[Post]:
        """Run the full research pipeline and return the posts it seeded.

        Discovers and scores topics, persists every one (so even rejected
        ideas are auditable), then turns the best into draft posts via the
        content agent. A single topic failing never aborts the run.
        """
        logger.info("=== Research pipeline starting ===")
        topics = self.discover(categories)
        logger.info("Discovered %d topic(s)", len(topics))

        selected = self.select_best(topics)

        if persist and self._db is not None:
            for topic in topics:
                try:
                    self._db.insert_topic(topic)
                except Exception:
                    logger.exception("Could not persist topic %s", topic.id)

        posts: list[Post] = []
        if generate_content and self._content_agent is not None:
            for topic in selected:
                post = topic.to_post()
                try:
                    self._content_agent.generate(post)
                    topic.mark(TopicStatus.USED)
                    posts.append(post)
                except Exception:
                    logger.exception("Content generation failed for topic %s", topic.id)
                    continue
                finally:
                    if persist and self._db is not None:
                        try:
                            self._db.upsert_topic(topic)
                        except Exception:
                            logger.exception("Could not update topic %s after content", topic.id)

        logger.info(
            "=== Research pipeline finished: %d selected, %d post(s) seeded ===",
            len(selected),
            len(posts),
        )
        return posts

    # --- Internals -------------------------------------------------------

    def _search_trends(self, categories: list[str]) -> str:
        """Run the web-search call and return the findings as text."""
        topic_count = max(self._cfg.topics_per_run * 2, 6)
        themes = ", ".join(categories)
        user_prompt = (
            f"Search the web for what's trending right now across these themes: {themes}.\n"
            f"Find at least {topic_count} distinct, specific, recent topics — new "
            "product launches, notable releases, emerging tools, or discussions "
            "gaining traction. For each, note what it is, why it's trending now, "
            "and include the source URL. Prefer the last few weeks; skip evergreen "
            "or generic topics."
        )

        base_user = {"role": "user", "content": user_prompt}
        messages = [base_user]
        response = None

        for _ in range(_MAX_CONTINUATIONS):
            try:
                response = self._client.messages.create(
                    model=self._cfg.anthropic_model,
                    max_tokens=8000,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "medium"},
                    system=[
                        {
                            "type": "text",
                            "text": self._system,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=[_WEB_SEARCH_TOOL],
                    messages=messages,
                )
            except anthropic.APIError:
                logger.exception("Web search for trending topics failed")
                raise

            # Server-side tool loop hit its cap; re-send to let it resume.
            if response.stop_reason == "pause_turn":
                messages = [base_user, {"role": "assistant", "content": response.content}]
                continue
            break

        return self._collect_text(response.content) if response else ""

    def _score_and_decide(self, findings: str, categories: list[str]) -> list[Topic]:
        """Turn raw findings into scored, assigned ``Topic`` objects."""
        user_prompt = (
            "Here are the trending topics found by research:\n\n"
            f"{findings}\n\n"
            "For each genuinely distinct topic, return a scored entry: a title, a "
            "short summary, the best-fit content pillar, the best platform, a "
            "concrete content angle, a brand-relevance score (0-100), a one-sentence "
            "rationale, and any supporting source URLs. Be honest with scores — not "
            "everything trending deserves a post."
        )

        try:
            response = self._client.messages.parse(
                model=self._cfg.anthropic_model,
                max_tokens=4000,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                system=[
                    {
                        "type": "text",
                        "text": self._system,
                        # Same cached brand brief as the search call.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}],
                output_format=TopicSlate,
            )
        except anthropic.APIError:
            logger.exception("Scoring trending topics failed")
            raise

        slate: TopicSlate | None = response.parsed_output
        if slate is None:
            raise RuntimeError(
                f"Claude returned no structured topics (stop_reason={response.stop_reason})"
            )

        topics = [self._to_topic(scored) for scored in slate.topics]

        usage = response.usage
        logger.info(
            "Scored %d topic(s) | cache_read=%s input=%s output=%s",
            len(topics),
            getattr(usage, "cache_read_input_tokens", 0),
            getattr(usage, "input_tokens", 0),
            getattr(usage, "output_tokens", 0),
        )
        return topics

    def _to_topic(self, scored: ScoredTopic) -> Topic:
        """Validate and normalise one structured result into a ``Topic``."""
        return Topic(
            title=scored.title.strip(),
            pillar=self._coerce_pillar(scored.pillar),
            platform=self._coerce_platform(scored.platform),
            summary=scored.summary.strip(),
            content_angle=scored.content_angle.strip(),
            relevance_score=_clamp_score(scored.relevance_score),
            rationale=scored.rationale.strip(),
            sources=[s for s in scored.sources if s],
        )

    def _coerce_pillar(self, value: str) -> str:
        """Snap the model's pillar to a known value, defaulting sensibly."""
        cleaned = (value or "").strip()
        if cleaned in _VALID_PILLARS:
            return cleaned
        for pillar in _VALID_PILLARS:
            if pillar.lower() == cleaned.lower():
                return pillar
        logger.warning("Unknown pillar %r; defaulting to %s", value, Pillar.TECH_LIFESTYLE.value)
        return Pillar.TECH_LIFESTYLE.value

    def _coerce_platform(self, value: str) -> str:
        """Snap the model's platform to a known value.

        Any real platform is honoured even if its credentials aren't set —
        the topic is still worth recording, and the publisher skips
        unconfigured platforms at posting time. Only genuinely invalid
        values fall back, preferring a configured platform.
        """
        cleaned = (value or "").strip().lower()
        if cleaned in _VALID_PLATFORMS:
            return cleaned
        fallback = (self._cfg.configured_platforms() or sorted(_VALID_PLATFORMS))[0]
        logger.warning("Unknown platform %r; defaulting to %s", value, fallback)
        return fallback

    @staticmethod
    def _collect_text(content) -> str:
        """Join the text blocks out of a (possibly tool-laden) response."""
        parts = []
        for block in content or []:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)


def _clamp_score(value: int) -> int:
    """Keep a relevance score within 0-100 (the model isn't constrained)."""
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0
