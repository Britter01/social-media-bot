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
clean, validated ``Topic`` objects.

No prompt caching here. Each call runs once per daily run, on different
models (Haiku then Sonnet), so there's no shared prefix to reuse within
the cache's 5-minute TTL — caching would only pay the write premium for a
read that never comes. (Caching lives in the content agent instead, whose
brand brief is large and reused across a batch of posts.)

Model choice (no Opus anywhere):
  * Discovery runs on ``model_fast`` (Haiku 4.5). It's a high-token,
    repetitive gather-and-summarise task: the server-side search does the
    real work, and the model just drives queries and lists what it found.
    Haiku is ~3-5x cheaper than Sonnet and ample for that.
  * Scoring runs on ``model_creative`` (Sonnet 4.6). Judging brand fit and
    choosing a pillar, platform, and angle is the creative/ideation step
    where a better model pays off — and it's a small, structured output, so
    the Sonnet premium applies to few tokens. The human approval gate is
    the final quality backstop downstream.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import anthropic
from pydantic import BaseModel, Field

from core.config import Config, config
from core.models import Pillar, Platform, Post, Topic, TopicStatus

logger = logging.getLogger(__name__)


def _is_safe_url(url: str) -> bool:
    """Return True only for well-formed http/https URLs.

    Rejects javascript:, data:, and other schemes that could be rendered
    as clickable links in the dashboard and used for phishing or XSS.
    """
    try:
        p = urlparse(url)
        return p.scheme in {"http", "https"} and bool(p.netloc)
    except Exception:
        return False


# Server-side web search tool. The 2026-02-09 version adds dynamic result
# filtering on Sonnet/Opus; on the Haiku tier we use here it runs as a plain
# web search (the search itself is server-side and tier-independent), which
# is all the discovery step needs.
_WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "allowed_callers": ["direct"],
}

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


_PLATFORM_FIT = {
    "instagram": "instagram (visual, lifestyle)",
    "twitter": "twitter (fast takes, news)",
    "linkedin": "linkedin (professional insight)",
    "youtube": "youtube (explainers, reviews)",
    "tiktok": "tiktok (short, energetic, native video)",
}


def _system_prompt(
    brand_name: str,
    founder: str,
    tagline: str,
    enabled_platforms: list[str] | None = None,
) -> str:
    """The large, stable brand brief that gets prompt-cached.

    ``enabled_platforms`` restricts which platforms the strategist may pick.
    Platforms paused via DISABLED_PLATFORMS are omitted entirely so the model
    never assigns a topic to a platform we aren't publishing to.
    """
    pillars = (
        "AI Guide — practical, accessible explainers that make AI useful today.\n"
        "Tech Lifestyle — how good technology fits into a well-lived life.\n"
        "Productivity — tools and systems that give people their time back.\n"
        "Fitness Tech — wearables, apps, and gear for a healthier life.\n"
        "Review — honest, hands-on verdicts. Pros, cons, who it's for."
    )
    allowed = [p for p in (enabled_platforms or list(_PLATFORM_FIT)) if p in _PLATFORM_FIT]
    if not allowed:  # defensive: never leave the model with no choice
        allowed = list(_PLATFORM_FIT)
    platform_fit = ", ".join(_PLATFORM_FIT[p] for p in allowed)
    quoted = ", ".join(f"'{p}'" for p in allowed)
    return (
        f"You are the content strategist for {brand_name}, founded by {founder}.\n"
        f'Tagline: "{tagline}"\n\n'
        "Your job is to find trending topics worth posting about and judge how "
        "well each fits the brand. Score for relevance to the audience: people "
        "who love technology and want it to genuinely improve their lives — no "
        "hype-chasers, no fake news, no clickbait.\n\n"
        "CONTENT PILLARS:\n"
        f"{pillars}\n\n"
        f"PLATFORM FIT: {platform_fit}.\n"
        f"You may ONLY assign a topic to one of these platforms: {quoted}. "
        "Never assign a topic to any other platform.\n\n"
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
        self._system = _system_prompt(
            cfg.brand_name,
            cfg.brand_founder,
            cfg.brand_tagline,
            enabled_platforms=cfg.enabled_platforms(),
        )

    # --- Public API ------------------------------------------------------

    def discover(self, categories: list[str] | None = None) -> list[Topic]:
        """Find trending topics and return them scored and assigned.

        Raises on hard API failures so the caller can decide what to do.
        """
        raw = categories or self._cfg.research_categories
        # Whitelist-validate against the configured set to prevent prompt injection
        # if categories are ever sourced from user input or an external API.
        valid_set = {c.lower() for c in self._cfg.research_categories}
        categories = [c for c in raw if c.lower() in valid_set] or list(
            self._cfg.research_categories
        )
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
        disabled = set(self._cfg.disabled_platforms)

        eligible = []
        for topic in topics:
            if topic.platform in disabled:
                # Platform paused via DISABLED_PLATFORMS — never surface it for
                # approval, generate content, or post it.
                topic.mark(TopicStatus.REJECTED)
            elif topic.relevance_score >= threshold:
                eligible.append(topic)
            else:
                topic.mark(TopicStatus.REJECTED)

        eligible.sort(key=lambda t: t.relevance_score, reverse=True)
        chosen = eligible[:cap]
        for topic in chosen:
            topic.mark(TopicStatus.PENDING_APPROVAL)
        return chosen

    def research(self, categories: list[str] | None = None, *, persist: bool = True) -> list[Topic]:
        """Discover, score, and persist topics; return the selected ones.

        Every topic is persisted (so even rejected ideas are auditable);
        the ones that clear the relevance bar are marked ``selected`` and
        returned. This does **not** generate content — with the approval
        gate on, selected topics wait here for a human to approve them.
        """
        logger.info("=== Research starting ===")
        topics = self.discover(categories)
        logger.info("Discovered %d topic(s)", len(topics))

        selected = self.select_best(topics)

        if persist and self._db is not None:
            for topic in topics:
                try:
                    self._db.insert_topic(topic)
                except Exception:
                    logger.exception("Could not persist topic %s", topic.id)

        logger.info("=== Research finished: %d topic(s) selected ===", len(selected))
        return selected

    def generate_content(self, topics: list[Topic], *, persist: bool = True) -> list[Post]:
        """Turn approved/selected topics into content-ready draft posts.

        Each topic becomes a draft post that the content agent fills in.
        A single topic failing never aborts the batch; successful ones are
        marked ``used``.
        """
        posts: list[Post] = []
        if self._content_agent is None:
            logger.warning("No content agent configured; cannot generate content")
            return posts

        configured = set(self._cfg.configured_platforms()) if self._cfg else set()
        disabled = set(self._cfg.disabled_platforms) if self._cfg else set()

        for topic in topics:
            # Safety net: never generate content for a paused platform, even if
            # an old topic for it slipped through approval before it was paused.
            if topic.platform in disabled:
                logger.info("Skipping topic %s — platform %r is disabled", topic.id, topic.platform)
                topic.mark(TopicStatus.REJECTED)
                if persist and self._db is not None:
                    try:
                        self._db.upsert_topic(topic)
                    except Exception:
                        logger.exception("Could not update disabled topic %s", topic.id)
                continue

            post = topic.to_post()
            try:
                self._content_agent.generate(post)
                topic.mark(TopicStatus.USED)
                if persist and self._db is not None:
                    self._db.insert(post)
                posts.append(post)

                # Cross-post: if Instagram is the target and Facebook is
                # configured, create an identical post for Facebook so the
                # same content reaches both audiences automatically.
                if (
                    post.platform == Platform.INSTAGRAM.value
                    and Platform.FACEBOOK.value in configured
                ):
                    fb_post = Post(
                        pillar=post.pillar,
                        platform=Platform.FACEBOOK.value,
                        topic=post.topic,
                        caption=post.caption,
                        hashtags=list(post.hashtags),
                        title=post.title,
                        thumbnail_url=post.thumbnail_url,
                    )
                    if persist and self._db is not None:
                        self._db.insert(fb_post)
                    posts.append(fb_post)
                    logger.info("Created Facebook cross-post for topic %s", topic.id)

            except Exception:
                logger.exception("Content generation failed for topic %s", topic.id)
                continue
            finally:
                if persist and self._db is not None:
                    try:
                        self._db.upsert_topic(topic)
                    except Exception:
                        logger.exception("Could not update topic %s after content", topic.id)

        return posts

    def generate_for_approved(self, *, persist: bool = True, limit: int = 50) -> list[Post]:
        """Generate content for topics a human has approved.

        Pulls ``approved`` topics from the database and runs them through
        :meth:`generate_content`. This is the second half of the gated
        flow, run after review.
        """
        if self._db is None:
            logger.warning("No database configured; cannot fetch approved topics")
            return []
        approved = self._db.topics_by_status(TopicStatus.APPROVED, limit=limit)
        if not approved:
            logger.info("No approved topics awaiting content")
            return []
        logger.info("Generating content for %d approved topic(s)", len(approved))
        return self.generate_content(approved, persist=persist)

    def weekly_strategy(
        self,
        competitor_urls: list[str] | None = None,
        target_count: int = 7,
    ) -> list[Topic]:
        """Research competitor patterns and return shaped topic ideas for the week.

        Runs a web search focused on what's working in the niche right now,
        optionally studying specific competitor accounts set via COMPETITOR_URLS.
        Returns ``target_count`` original topics dropped into the approval queue
        — same flow as the daily research pipeline, but driven by virality
        patterns rather than just trending news.

        All ideas are tailored to the brand voice from the system prompt.
        Competitors are studied for patterns only — no content is copied.
        """
        findings = self._search_competitor_patterns(competitor_urls or [], target_count)
        if not findings.strip():
            logger.warning(
                "Weekly strategy: web search returned no findings; falling back to daily discovery"
            )
            return self.discover()
        topics = self._score_and_decide(findings, self._cfg.research_categories)
        # Select the top ideas and persist to the approval queue
        best = self.select_best(topics, limit=target_count)
        if self._db is not None:
            for topic in best:
                try:
                    self._db.insert_topic(topic)
                except Exception:
                    logger.exception("Could not persist weekly strategy topic %s", topic.id)
        logger.info("Weekly strategy: %d topic(s) queued for approval", len(best))
        return best

    def run(
        self,
        categories: list[str] | None = None,
        *,
        persist: bool = True,
    ) -> list[Post]:
        """Run research, then generate content unless approval is required.

        With ``require_topic_approval`` on (the default), selected topics
        are left awaiting review and no posts are produced — call
        :meth:`generate_for_approved` after a human approves them. With it
        off, the best topics are turned into draft posts immediately.
        """
        selected = self.research(categories, persist=persist)

        if self._cfg.require_topic_approval:
            logger.info(
                "%d topic(s) awaiting approval — review them before they post "
                "(e.g. `python -m scripts.review_topics`)",
                len(selected),
            )
            return []

        posts = self.generate_content(selected, persist=persist)
        logger.info("Research seeded %d post(s)", len(posts))
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
                    # Fast tier (Haiku): discovery is a cheap, high-token
                    # gather-and-summarise pass over web results. No thinking
                    # or effort params — Haiku 4.5 doesn't support adaptive
                    # thinking or the effort parameter (they'd 400), and this
                    # task doesn't need them anyway.
                    model=self._cfg.model_fast,
                    max_tokens=8000,
                    # No prompt caching here: discovery is a single call per
                    # daily run, so there's nothing to reuse within the cache's
                    # 5-minute TTL — a cache write (~1.25x) would never be read
                    # back. (The brand brief is also too short to cache anyway.)
                    system=self._system,
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
                # Creative tier (Sonnet): scoring + pillar/platform/angle is
                # the judgment step. Small structured output, so the premium
                # applies to few tokens.
                model=self._cfg.model_creative,
                max_tokens=4000,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                # No prompt caching: also a single call per daily run, and on a
                # different model than discovery, so there's no shared prefix to
                # reuse. The big input here (the findings) varies every run.
                system=self._system,
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
            sources=[s for s in scored.sources if _is_safe_url(s)],
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

    def _search_competitor_patterns(self, competitor_urls: list[str], target_count: int) -> str:
        """Elite-strategist web search: viral patterns in the niche + competitor intel.

        The five cross-cutting patterns proven to drive growth in this space are
        embedded directly as output requirements so every idea inherits them:
          1. One-line repeatable promise — what the post will deliver, in one sentence
          2. "Use it now" takeaway — practical action, not just news
          3. Core asset repurposable across carousel → newsletter → short-form video
          4. Founder-visible angle — honest, disclosed, trust-building framing
          5. Consistent tone — warm, clear, confident; the brand voice is the moat
        """
        safe_urls = [u for u in competitor_urls if _is_safe_url(u)][:5]
        url_block = ""
        if safe_urls:
            url_block = (
                "\n\nVisit these reference accounts and study what's performed best recently "
                "— look for hook structures, format types (carousel, reel, talking-head, "
                "tutorial), and the exact 'use it now' angle that drives saves and shares. "
                "Study patterns only; do not copy specific content:\n"
                + "\n".join(f"- {u}" for u in safe_urls)
            )

        themes = ", ".join(self._cfg.research_categories)
        user_prompt = (
            "You are an elite social media strategist. Your task is to find what is "
            f"working RIGHT NOW in these niches: {themes}.\n\n"
            "Search the web for:\n"
            "• The highest-engagement posts from the last 2 weeks — what made them "
            "break through? Look for specific headlines, format types, and hook structures.\n"
            "• Emerging topics with clear rising traction that aren't yet saturated\n"
            "• Hook patterns that earn saves and shares, not just likes: list-based, "
            "contrast ('X vs Y'), counterintuitive claim, how-to step-by-step, "
            "before/after transformation, 'I tried X so you don't have to'\n"
            "• The emotional driver: curiosity gap, aspiration, fear-of-missing-out, "
            "surprise, or recognition ('that's exactly my problem')\n"
            f"{url_block}\n\n"
            f"Extract exactly {target_count} specific, original post ideas. "
            "For each idea, include ALL of the following — these are non-negotiable:\n\n"
            "  ONE-LINE PROMISE: what the audience will walk away with (e.g. "
            "'You'll know the 3 AI tools saving 5 hours a week for knowledge workers')\n"
            "  USE IT NOW TAKEAWAY: the immediate, practical action — not just news, "
            "not just inspiration. What can they do or try within the next 10 minutes?\n"
            "  HOOK / HEADLINE: the exact opening line or carousel cover headline\n"
            "  REPURPOSE PATH: how this one idea maps to carousel → reel/short → newsletter\n"
            "  EMOTIONAL TRIGGER: which emotion drives the share/save, and why\n"
            "  VIRAL PATTERN: which proven structure it uses (name the pattern)\n"
            "  SOURCE URLs: where you found the evidence that this topic has traction\n\n"
            "Be specific. Vague ideas ('AI is changing everything') are worthless. "
            "Great ideas name the tool, the use case, the person, or the number."
        )

        base_user = {"role": "user", "content": user_prompt}
        messages = [base_user]
        response = None

        for _ in range(_MAX_CONTINUATIONS):
            try:
                response = self._client.messages.create(
                    model=self._cfg.model_fast,
                    max_tokens=8000,
                    system=self._system,
                    tools=[_WEB_SEARCH_TOOL],
                    messages=messages,
                )
            except anthropic.APIError:
                logger.exception("Weekly strategy web search failed")
                raise

            if response.stop_reason == "pause_turn":
                messages = [base_user, {"role": "assistant", "content": response.content}]
                continue
            break

        return self._collect_text(response.content) if response else ""

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
