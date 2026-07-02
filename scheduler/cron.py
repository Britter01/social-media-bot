"""Cron entry point — runs the whole pipeline on a schedule.

Two recurring jobs, driven by APScheduler's blocking scheduler (the
process the ``Procfile`` worker runs):

  * ``run_content_pipeline`` (daily) — for each pillar, pick a platform,
    generate caption + hashtags, generate media, choose an optimal time,
    and persist the post as ``scheduled``.

  * ``run_publisher`` (every few minutes) — find posts whose scheduled
    time has passed and publish them.

Every stage is wrapped so a single post or platform failing is logged and
isolated, never crashing the worker. Media generation is best-effort:
if thumbnail/video creation fails, the post still goes out as text where
the platform allows it.

Agent imports are intentionally lazy (inside each function) so that this
module can be imported by the dashboard without requiring the full set of
worker dependencies (anthropic, google-genai, apscheduler, etc.).
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import UTC, datetime, timedelta
from itertools import cycle

from core.config import config, configure_logging
from core.database import get_database
from core.models import Platform, Post, PostStatus

logger = logging.getLogger("scheduler.cron")

# Platforms that need a video asset before publishing.
_VIDEO_PLATFORMS = {Platform.YOUTUBE.value, Platform.TIKTOK.value}
_CAROUSEL_PLATFORMS = frozenset({Platform.INSTAGRAM.value, Platform.FACEBOOK.value})


def _round_robin_platforms(pillars: list[str], platforms: list[str]) -> list[tuple]:
    """Pair each pillar with a platform, rotating through the platforms."""
    if not platforms:
        return []
    wheel = cycle(platforms)
    return [(pillar, next(wheel)) for pillar in pillars]


def run_content_pipeline(manual: bool = False) -> str:
    """Generate, enrich, and schedule a batch of posts.

    Returns a short diagnostic string so the dashboard Pipeline tab can show
    what happened (agent status, post count, image generation result).
    When *manual* is True, posts land in ``manual_ready`` status (Generated tab)
    rather than being auto-scheduled.
    """
    from agents.carousel_agent import CarouselAgent
    from agents.content_agent import ContentAgent
    from agents.quality_agent import QualityAgent, QualityError
    from agents.reels_agent import ReelsAgent
    from agents.scheduler_agent import SchedulerAgent
    from agents.thumbnail_agent import ThumbnailAgent
    from agents.video_agent import VideoAgent

    logger.info("=== Content pipeline starting ===")
    try:
        db = get_database()
        content_agent = ContentAgent()
        scheduler_agent = SchedulerAgent()
    except Exception:
        logger.exception("Content pipeline could not initialise; skipping run")
        return "pipeline failed to initialise"

    thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
    video_agent = _safe_init(VideoAgent, "video")
    quality_agent = _safe_init(QualityAgent, "quality")
    carousel_agent = _safe_init(CarouselAgent, "carousel")
    reels_agent = _safe_init(ReelsAgent, "reels")

    platforms = config.configured_platforms() or config.platforms
    pairings = _round_robin_platforms(config.content_pillars, platforms)

    # Seed last_slot from any posts already in the queue so new posts are
    # scheduled AFTER everything that's already there, not on top of it.
    last_slot: dict[str, datetime] = db.latest_scheduled_time_by_platform()

    # Ensure posts_per_run covers at least one post per configured platform so
    # every platform gets a turn every pipeline run (not just the first one in
    # the round-robin). posts_per_run=0 means "process all pairings".
    _limit = max(config.posts_per_run, len(platforms)) if config.posts_per_run else len(pairings)

    created = 0
    no_image = 0
    for pillar, platform in pairings[:_limit]:
        post = Post(pillar=pillar, platform=platform)
        try:
            db.insert(post)
            content_agent.generate(post)
            if quality_agent:
                quality_agent.fix_text(post)
            try:
                _apply_media(post, thumbnail_agent, video_agent, quality_agent, carousel_agent)
            except QualityError as exc:
                logger.warning("QC rejected post %s: %s", post.id, exc)
                db.update_status(post, PostStatus.FAILED, error=f"QC: {exc}")
                continue
            # Track posts that have no image after media generation
            has_image = bool(post.thumbnail_url) or bool(post.slides)
            if not has_image:
                no_image += 1
            if manual:
                post.mark(PostStatus.MANUAL_READY)
            else:
                after = last_slot.get(platform)
                scheduler_agent.schedule(post, after=after)
                last_slot[platform] = post.scheduled_time
            db.upsert(post)
            created += 1

            # Generate Reel twin(s) for any carousel (IG or FB).
            if not manual and post.platform in _CAROUSEL_PLATFORMS and reels_agent and post.slides:
                _configured = set(config.configured_platforms())
                _reel_count = _create_reel_twins(
                    post,
                    reels_agent,
                    scheduler_agent,
                    db,
                    last_slot,
                    _configured,
                    persist_insert=True,
                )
                if _reel_count:
                    logger.info("Created %d reel(s) for post %s", _reel_count, post.id)
        except Exception as exc:
            logger.exception("Failed building post for %s/%s", pillar, platform)
            try:
                db.update_status(
                    post,
                    PostStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}"[:400],
                )
            except Exception:
                logger.exception("Could not persist failure for post %s", post.id)

    # Build a human-readable summary for the dashboard Pipeline tab.
    parts = [f"created {created} post(s)"]
    if no_image:
        reasons = []
        if carousel_agent is None:
            reasons.append("carousel agent unavailable — check ANTHROPIC_API_KEY in Railway")
        if thumbnail_agent is None:
            reasons.append("thumbnail agent unavailable — check GOOGLE_API_KEY in Railway")
        if not reasons:
            reasons.append(
                "Imagen ran but images did not upload — "
                "check the 'media' bucket exists in Supabase Storage"
            )
        parts.append(f"{no_image} post(s) have no image: {'; '.join(reasons)}")
    logger.info("=== Content pipeline finished: %s ===", " — ".join(parts))
    return " — ".join(parts)


def run_research_pipeline() -> str:
    """Discover and store trending topics for review.

    The research agent finds, scores, and persists topics. With the
    approval gate on (the default), selected topics wait as ``selected``
    for a human to review (``scripts/review_topics``) — nothing is posted
    here. With the gate off, ``run()`` returns content-ready posts, which
    we finalise and schedule immediately.
    """
    from agents.content_agent import ContentAgent
    from agents.research_agent import ResearchAgent
    from agents.scheduler_agent import SchedulerAgent

    logger.info("=== Research pipeline starting ===")
    try:
        db = get_database()
        content_agent = ContentAgent()
        scheduler_agent = SchedulerAgent()
        research_agent = ResearchAgent(content_agent=content_agent, db=db)
    except Exception:
        logger.exception("Research pipeline could not initialise; skipping run")
        return "pipeline failed to initialise"

    try:
        posts = research_agent.run()
    except Exception:
        logger.exception("Research pipeline failed during discovery; skipping run")
        return "research discovery failed"

    scheduled, summary = _finalise_posts(posts, db, scheduler_agent)
    logger.info("=== Research pipeline finished: %s ===", summary)
    return summary


def run_approved_pipeline() -> None:
    """Turn human-approved topics into scheduled posts.

    The second half of the gated research flow: pulls topics a human
    marked ``approved``, generates content, then finalises and schedules
    each. Runs on a short interval so approvals go live promptly.
    """
    from agents.content_agent import ContentAgent
    from agents.research_agent import ResearchAgent
    from agents.scheduler_agent import SchedulerAgent

    try:
        db = get_database()
        content_agent = ContentAgent()
        scheduler_agent = SchedulerAgent()
        research_agent = ResearchAgent(content_agent=content_agent, db=db)
    except Exception:
        logger.exception("Approved-topic pipeline could not initialise; skipping run")
        return

    try:
        posts = research_agent.generate_for_approved()
    except Exception:
        logger.exception("Approved-topic pipeline failed; skipping run")
        return

    if not posts:
        return

    logger.info("Scheduling %d post(s) from approved topics", len(posts))
    scheduled, summary = _finalise_posts(posts, db, scheduler_agent, persist_insert=False)
    logger.info("Approved-topic pipeline finished: %s", summary)


def _finalise_posts(
    posts: list[Post], db, scheduler_agent, *, persist_insert: bool = True
) -> tuple[int, str]:
    """Add media, pick a slot, and persist each post.

    Returns ``(scheduled_count, diagnostic_string)`` so callers can surface
    agent-availability warnings in the Pipeline tab.

    Instagram and Facebook posts are always produced as carousels (see
    _apply_media).  All other platforms get a single image or video.

    Failure-isolated per post — one failing post is marked failed and the
    rest continue. Set persist_insert=False when the posts are already in the
    database (e.g. inserted by generate_for_approved).
    """
    from agents.carousel_agent import CarouselAgent
    from agents.quality_agent import QualityAgent, QualityError
    from agents.reels_agent import ReelsAgent
    from agents.thumbnail_agent import ThumbnailAgent
    from agents.video_agent import VideoAgent

    thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
    video_agent = _safe_init(VideoAgent, "video")
    quality_agent = _safe_init(QualityAgent, "quality")
    carousel_agent = _safe_init(CarouselAgent, "carousel")
    reels_agent = _safe_init(ReelsAgent, "reels")

    # Seed from the DB so new posts land after whatever's already queued.
    last_slot: dict[str, datetime] = db.latest_scheduled_time_by_platform()
    scheduled = 0
    no_image = 0

    for post in posts:
        try:
            if quality_agent:
                quality_agent.fix_text(post)
            try:
                _apply_media(post, thumbnail_agent, video_agent, quality_agent, carousel_agent)
            except QualityError as exc:
                logger.warning("QC rejected post %s: %s", post.id, exc)
                post.mark(PostStatus.FAILED, error=f"QC: {exc}")
                if persist_insert:
                    db.insert(post)
                else:
                    db.upsert(post)
                continue
            if not (post.thumbnail_url or post.slides):
                no_image += 1
            after = last_slot.get(post.platform)
            scheduler_agent.schedule(post, after=after)
            last_slot[post.platform] = post.scheduled_time
            if persist_insert:
                db.insert(post)
            else:
                db.upsert(post)
            scheduled += 1

            # Generate Reel twin(s) for any carousel (IG or FB).
            if post.platform in _CAROUSEL_PLATFORMS and reels_agent and post.slides:
                _configured = set(config.configured_platforms())
                _create_reel_twins(
                    post,
                    reels_agent,
                    scheduler_agent,
                    db,
                    last_slot,
                    _configured,
                    persist_insert=persist_insert,
                )

        except Exception as exc:
            logger.exception("Failed finalising post %s", post.id)
            try:
                db.update_status(
                    post,
                    PostStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}"[:400],
                )
            except Exception:
                logger.exception("Could not persist failure for post %s", post.id)

    parts = [f"scheduled {scheduled} post(s)"]
    if no_image:
        reasons = []
        if carousel_agent is None:
            reasons.append("carousel agent unavailable — check ANTHROPIC_API_KEY in Railway")
        if thumbnail_agent is None:
            reasons.append("thumbnail agent unavailable — check GOOGLE_API_KEY in Railway")
        if not reasons:
            reasons.append(
                "Imagen ran but images did not upload — "
                "check the 'media' bucket exists in Supabase Storage"
            )
        parts.append(f"{no_image} post(s) have no image: {'; '.join(reasons)}")
    return scheduled, " — ".join(parts)


def _create_reel_twins(
    carousel_post: Post,
    reels_agent,
    scheduler_agent,
    db,
    last_slot: dict,
    configured: set,
    *,
    persist_insert: bool = True,
) -> int:
    """Generate Reel post(s) from a finished carousel post and persist them.

    For an Instagram carousel: creates one Instagram Reel + one Facebook Reel
    (so both platforms get video coverage from a single video render).
    For a Facebook carousel (e.g. cross-posted from LinkedIn): creates only a
    Facebook Reel (the Instagram flow already handles the IG twin).

    The video is rendered once and both posts share the same Supabase URL.
    Returns the number of Reel posts successfully created.
    """
    if carousel_post.platform == Platform.INSTAGRAM.value:
        reel_platforms = [Platform.INSTAGRAM.value]
        if Platform.FACEBOOK.value in configured:
            reel_platforms.append(Platform.FACEBOOK.value)
    else:
        reel_platforms = [carousel_post.platform]

    try:
        video_url = reels_agent.generate_video_url(carousel_post)
    except Exception:
        logger.exception("ReelsAgent failed for post %s", carousel_post.id)
        return 0

    if not video_url:
        return 0

    count = 0
    for reel_plat in reel_platforms:
        try:
            reel = Post(
                pillar=carousel_post.pillar,
                platform=reel_plat,
                topic=carousel_post.topic,
                caption=carousel_post.caption,
                hashtags=list(carousel_post.hashtags),
                title=carousel_post.title,
                video_url=video_url,
                post_type="reel",
                status=PostStatus.CONTENT_READY.value,
            )
            scheduler_agent.schedule(reel, after=last_slot.get(reel_plat))
            last_slot[reel_plat] = reel.scheduled_time
            if persist_insert:
                db.insert(reel)
            else:
                db.upsert(reel)
            count += 1
            logger.info(
                "Created %s reel (id=%s) for carousel post %s", reel_plat, reel.id, carousel_post.id
            )
        except Exception:
            logger.exception("Failed persisting %s reel for post %s", reel_plat, carousel_post.id)

    return count


def _apply_media(post: Post, thumbnail_agent, video_agent, quality_agent, carousel_agent) -> None:
    """Assign media to *post* in place.

    Instagram and Facebook are **carousel-only**: they always get a 4-slide
    text-based carousel and never fall back to a single-image "standard" post.
    If the carousel can't be built the failure propagates so the caller marks
    the post FAILED — we never publish an off-brand single-image post on IG/FB.
    All other platforms get a single image or video via _generate_media.
    """
    if post.platform in _CAROUSEL_PLATFORMS:
        if not carousel_agent:
            raise RuntimeError(
                "carousel agent unavailable for IG/FB — check ANTHROPIC_API_KEY in Railway"
            )
        carousel = carousel_agent.create_from_post(post, quality_agent=quality_agent)
        if not carousel.slides:
            raise RuntimeError("carousel returned 0 slides")
        post.post_type = carousel.post_type
        post.slides = carousel.slides
        post.thumbnail_url = carousel.thumbnail_url
        post.title = carousel.title or post.title
        post.caption = carousel.caption or post.caption
        post.mark(PostStatus.MEDIA_READY)
        return

    _generate_media(post, thumbnail_agent, video_agent, quality_agent)


def _generate_media(post: Post, thumbnail_agent, video_agent, quality_agent=None) -> None:
    """Generate media assets for *post*.

    Thumbnail: Imagen is called once. If a quality_agent is provided the
    overlay is QC-checked in memory before uploading — re-applying the
    overlay (free Pillow) once if it fails — so no Imagen credit is ever
    wasted on a branding problem.  Raises QualityError if both overlay
    attempts fail; the caller should mark the post failed.

    Video: best-effort, never raises.
    """
    needs_video = post.platform in _VIDEO_PLATFORMS

    if thumbnail_agent is not None:
        try:
            raw_bytes = thumbnail_agent.generate_raw(post)
        except Exception as exc:
            logger.exception("Imagen generation failed for post %s", post.id)
            raw_bytes = None
            _append_error(post, f"imagen: {type(exc).__name__}: {str(exc)[:300]}")

        if raw_bytes is not None:
            final_bytes = thumbnail_agent.apply_overlay(raw_bytes)

            if quality_agent is not None:
                try:
                    quality_agent.check_image_bytes(post, final_bytes)
                except Exception as _qc_exc:
                    # Free retry: re-apply overlay (no new Imagen call).
                    from agents.quality_agent import QualityError

                    if not isinstance(_qc_exc, QualityError):
                        raise
                    final_bytes = thumbnail_agent.apply_overlay(raw_bytes)
                    quality_agent.check_image_bytes(post, final_bytes)

            try:
                thumbnail_agent.upload(post, final_bytes)
                # Upload succeeded — clear any prior Imagen/upload error note
                if post.error and post.error.startswith(("imagen:", "upload:")):
                    post.error = None
            except Exception as exc:
                logger.exception("Thumbnail upload failed for post %s", post.id)
                _append_error(post, f"upload: {type(exc).__name__}: {str(exc)[:300]}")

    if needs_video and video_agent is not None:
        try:
            video_agent.generate(post)
        except Exception:
            logger.exception("Video generation failed for post %s", post.id)


def run_image_refresh() -> str | None:
    """Regenerate thumbnails and carousel slides that are missing the brand overlay.

    Runs nightly at 02:00 — after the Imagen quota resets (midnight UTC) —
    to pick up any slides that were skipped due to rate limits during the day.
    Processes both scheduled posts (missing image) and failed posts (missing
    image only — failed posts that already have an image are left alone so
    the user can retry them manually).
    """
    import uuid as _uuid

    from supabase import create_client

    from agents.carousel_agent import CarouselAgent
    from agents.scheduler_agent import SchedulerAgent
    from agents.thumbnail_agent import ThumbnailAgent

    logger.info("=== Image refresh starting ===")
    try:
        sb = create_client(config.supabase_url, config.supabase_key)
        thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
        carousel_agent = _safe_init(CarouselAgent, "carousel")
        scheduler_agent = SchedulerAgent()

        refreshed = 0

        _carousel_pf = list(_CAROUSEL_PLATFORMS)

        def _carousel_rows(status: str) -> list:
            # Any IG/FB post that still lacks slides — regardless of whether it
            # was saved as 'standard' (e.g. an older single-image row) or
            # 'carousel'. These are rebuilt as 4-slide text carousels, never as
            # single-image posts.
            #
            # Two queries are needed: (a) thumbnail_url IS NULL — the normal
            # failed case; (b) thumbnail_url does NOT start with 'http' — posts
            # that were previously saved with a local /tmp/ path because Supabase
            # was temporarily unconfigured. NULL rows are excluded from query (b)
            # automatically because NOT LIKE on NULL returns NULL (falsy) in SQL.
            null_rows = (
                sb.table("posts")
                .select("*")
                .in_("platform", _carousel_pf)
                .eq("status", status)
                .is_("thumbnail_url", "null")
                .execute()
                .data
                or []
            )
            local_rows = (
                sb.table("posts")
                .select("*")
                .in_("platform", _carousel_pf)
                .eq("status", status)
                .not_.like("thumbnail_url", "http%")
                .execute()
                .data
                or []
            )
            seen = {r["id"] for r in null_rows}
            return null_rows + [r for r in local_rows if r["id"] not in seen]

        def _standard_rows(status: str) -> list:
            # Single-image platforms only (Twitter/LinkedIn/YouTube/TikTok).
            # IG/FB are deliberately excluded — they are carousel-only.
            return (
                sb.table("posts")
                .select("*")
                .not_.in_("platform", _carousel_pf)
                .eq("post_type", "standard")
                .eq("status", status)
                .is_("thumbnail_url", "null")
                .execute()
                .data
                or []
            )

        logger.info(
            "Image refresh: carousel_agent=%s thumbnail_agent=%s",
            carousel_agent is not None,
            thumbnail_agent is not None,
        )

        # --- Carousels (scheduled + failed with no image) ---
        if carousel_agent:
            for status in ("scheduled", "failed"):
                rows = _carousel_rows(status)
                logger.info("Image refresh: %d %s carousel(s) need images", len(rows), status)
                for c in rows:
                    try:
                        source = Post(
                            id=c["id"],
                            pillar=c["pillar"],
                            platform=c["platform"],
                            topic=c.get("topic", ""),
                            title=c.get("title", ""),
                            caption=c.get("caption", ""),
                            hashtags=list(c.get("hashtags") or []),
                        )
                        new_id = str(_uuid.uuid4())
                        plan = carousel_agent._plan_carousel(source)
                        slides = carousel_agent._generate_slides(new_id, source, plan)
                        update: dict = {
                            # Force carousel type so any older single-image
                            # IG/FB row is converted into a proper carousel.
                            "post_type": "carousel",
                            "title": plan.cover_headline,
                            "slides": slides,
                            "thumbnail_url": slides[0]["image_url"] if slides else None,
                        }
                        if status == "failed" and slides:
                            scheduler_agent.schedule(source)
                            update["status"] = "scheduled"
                            update["scheduled_time"] = source.scheduled_time.isoformat()
                            update["error"] = None
                        sb.table("posts").update(update).eq("id", c["id"]).execute()
                        refreshed += 1
                        logger.info(
                            "Refreshed %s carousel %s (%d slides)",
                            status,
                            c["id"][:8],
                            len(slides),
                        )
                    except Exception:
                        logger.exception("Failed refreshing carousel %s", c.get("id", "?")[:8])

        else:
            logger.warning("Image refresh: carousel_agent unavailable — skipping carousels")

        # --- Standard posts (scheduled + failed with no image) ---
        if thumbnail_agent:
            for status in ("scheduled", "failed"):
                rows = _standard_rows(status)
                logger.info("Image refresh: %d %s standard post(s) need images", len(rows), status)
                for p in rows:
                    try:
                        post_obj = Post(
                            id=p["id"],
                            pillar=p["pillar"],
                            platform=p["platform"],
                            topic=p.get("topic", ""),
                            title=p.get("title", ""),
                            caption=p.get("caption", ""),
                            hashtags=list(p.get("hashtags") or []),
                        )
                        thumbnail_agent.generate(post_obj)
                        update = {"thumbnail_url": post_obj.thumbnail_url}
                        if status == "failed" and post_obj.thumbnail_url:
                            scheduler_agent.schedule(post_obj)
                            update["status"] = "scheduled"
                            update["scheduled_time"] = post_obj.scheduled_time.isoformat()
                            update["error"] = None
                        sb.table("posts").update(update).eq("id", p["id"]).execute()
                        refreshed += 1
                        logger.info("Refreshed %s thumbnail for post %s", status, p["id"][:8])
                    except Exception:
                        logger.exception(
                            "Failed refreshing thumbnail for post %s", p.get("id", "?")[:8]
                        )

        parts = [f"refreshed {refreshed} asset(s)"]
        if carousel_agent is None:
            parts.append("carousel agent unavailable — check ANTHROPIC_API_KEY in Railway")
        if thumbnail_agent is None:
            parts.append("thumbnail agent unavailable — check GOOGLE_API_KEY in Railway")
        summary = " — ".join(parts)
        logger.info("=== Image refresh finished: %s ===", summary)
        return summary
    except Exception as exc:
        logger.exception("Image refresh could not initialise; skipping run")
        return f"image refresh failed to initialise: {type(exc).__name__}: {exc}"[:400]


def _reset_stuck_publishing(threshold_minutes: int = 15) -> int:
    """Reset posts stuck in 'publishing' back to 'scheduled'.

    A post stuck in 'publishing' for longer than threshold_minutes was likely
    abandoned mid-run (process crash, network error, upsert failure). Resetting
    it back to 'scheduled' allows the next publisher run to pick it up.
    """
    from supabase import create_client

    try:
        sb = create_client(config.supabase_url, config.supabase_key)
        cutoff = (datetime.now(UTC) - timedelta(minutes=threshold_minutes)).isoformat()
        resp = (
            sb.table("posts")
            .update({"status": PostStatus.SCHEDULED.value})
            .eq("status", PostStatus.PUBLISHING.value)
            .lt("updated_at", cutoff)
            .execute()
        )
        n = len(resp.data) if resp.data else 0
        if n:
            logger.warning("Reset %d stuck 'publishing' post(s) back to 'scheduled'", n)
        return n
    except Exception:
        logger.exception("Failed to reset stuck publishing posts")
        return 0


def run_publisher(ignore_platform_pause: bool = False) -> None:
    """Publish every post whose scheduled time has arrived.

    *ignore_platform_pause* is True when triggered by an explicit manual
    'Publish Now' from the dashboard — those should always go through.
    """
    from agents.publisher_agent import PublisherAgent

    try:
        db = get_database()
        publisher = PublisherAgent()
    except Exception:
        logger.exception("Publisher could not initialise; skipping run")
        return

    _reset_stuck_publishing()
    due = db.due_for_publishing(now=datetime.now(UTC))
    if not due:
        logger.debug("No posts due for publishing")
        return

    logger.info("Publishing %d due post(s)", len(due))
    _configured = set(config.configured_platforms())
    for post in due:
        if not ignore_platform_pause and _is_platform_paused(post.platform):
            logger.info(
                "Platform %s is paused — skipping post %s (resume to publish automatically)",
                post.platform,
                post.id,
            )
            continue
        # Scheduled runs never attempt platforms with no credentials — the
        # attempt can only fail. A manual Publish Now still goes through so
        # the user sees the real (missing-credentials) error instead of
        # silence.
        if not ignore_platform_pause and post.platform not in _configured:
            logger.info(
                "Platform %s has no API credentials — leaving post %s scheduled",
                post.platform,
                post.id,
            )
            continue

        try:
            if not db.claim_for_publishing(post):
                continue
        except Exception:
            logger.exception("Could not claim post %s; skipping", post.id)
            continue

        try:
            publisher.publish(post)
        except Exception:
            logger.exception("Publish failed for post %s; continuing", post.id)
        finally:
            # Retry the persist: if the platform API call succeeded but this
            # write is lost, the post is reset to 'scheduled' 15 minutes later
            # and published a second time.
            for _attempt in range(3):
                try:
                    db.upsert(post)
                    break
                except Exception:
                    if _attempt == 2:
                        logger.exception("Could not persist post %s after publish", post.id)
                    else:
                        import time

                        time.sleep(2 * (_attempt + 1))


def run_qc_retry() -> None:
    """Regenerate thumbnails and re-run QC for posts that failed the image check.

    Runs hourly. Finds posts whose error field starts with 'QC:', regenerates
    the thumbnail, and reschedules any that pass the second check. Posts that
    fail again are left as failed with an updated error message.
    """
    from supabase import create_client

    from agents.carousel_agent import CarouselAgent
    from agents.quality_agent import QualityAgent, QualityError
    from agents.scheduler_agent import SchedulerAgent
    from agents.thumbnail_agent import ThumbnailAgent

    try:
        sb = create_client(config.supabase_url, config.supabase_key)
        thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
        quality_agent = _safe_init(QualityAgent, "quality")
        carousel_agent = _safe_init(CarouselAgent, "carousel")
        scheduler_agent = SchedulerAgent()
        db = get_database()
    except Exception:
        logger.exception("QC retry could not initialise; skipping run")
        return

    rows = (
        sb.table("posts").select("*").eq("status", "failed").like("error", "QC:%").execute().data
        or []
    )

    if not rows:
        return

    logger.info("QC retry: %d post(s) to recheck", len(rows))
    retried = 0
    for row in rows:
        post = Post.from_row(row)
        try:
            # IG/FB are carousel-only — rebuild a text carousel rather than a
            # single image, otherwise QC retry would resurrect the old design.
            if post.platform in _CAROUSEL_PLATFORMS:
                if not carousel_agent:
                    logger.warning("QC retry: carousel agent unavailable for %s", post.id[:8])
                    continue
                carousel = carousel_agent.create_from_post(post)
                if not carousel.slides:
                    logger.warning(
                        "QC retry: carousel rebuild produced 0 slides for %s", post.id[:8]
                    )
                    continue
                post.post_type = carousel.post_type
                post.slides = carousel.slides
                post.thumbnail_url = carousel.thumbnail_url
                post.title = carousel.title or post.title
                post.caption = carousel.caption or post.caption
                post.error = None
            else:
                try:
                    _generate_media(
                        post, thumbnail_agent, video_agent=None, quality_agent=quality_agent
                    )
                except QualityError as exc:
                    logger.warning(
                        "QC retry: post %s still failing after regen: %s", post.id[:8], exc
                    )
                    post.mark(PostStatus.FAILED, error=f"QC (retry): {exc}")
                    sb.table("posts").update({"error": post.error}).eq("id", post.id).execute()
                    continue

            scheduler_agent.schedule(post)
            db.upsert(post)
            retried += 1
            logger.info("QC retry: post %s rescheduled", post.id[:8])
        except Exception:
            logger.exception("QC retry failed for post %s", post.id)

    logger.info("QC retry finished: %d post(s) rescheduled", retried)


def run_analytics() -> str | None:
    """Fetch engagement metrics for posts at 24h and 7d after publish.

    Returns a short human-readable summary (posts checked, snapshots stored,
    and the most common failure reason) so the dashboard can show *why* a
    fetch returned nothing instead of leaving the tab blank.
    """
    from agents.analytics_agent import AnalyticsAgent

    logger.info("=== Analytics fetch starting ===")
    try:
        agent = AnalyticsAgent()
        n24 = agent.run_snapshot("24h")
        n7d = agent.run_snapshot("7d")
        nb = agent.run_backfill()
        nv = agent.run_views_refresh()
        logger.info(
            "=== Analytics done: %d 24h, %d 7d, %d backfilled, %d views patched ===",
            n24,
            n7d,
            nb,
            nv,
        )
        return agent.summary()
    except Exception as exc:
        logger.exception("Analytics fetch failed")
        return f"run failed: {exc}"


def run_diagnostics() -> str:
    """Self-check the worker's runtime environment and return a report.

    Runs inside the worker process (which has its OWN Railway environment
    variables, separate from the dashboard) so the report reflects exactly
    what the pipeline can and cannot do. Checks, in order:

      * which API keys are present (values never shown),
      * whether each media agent can initialise (the real reason posts come
        out without images is almost always an agent failing to start here),
      * whether a tiny test object can be written to Supabase Storage.

    The result is a single line surfaced on the dashboard Pipeline tab.
    """
    logger.info("=== Diagnostics starting ===")
    write_platform_status()
    parts: list[str] = []

    # 1. Config presence (never reveal the actual secret values).
    keys = {
        "ANTHROPIC_API_KEY": bool(config.anthropic_api_key),
        "GOOGLE_API_KEY": bool(config.google_api_key),
        "HIGGSFIELD_API_KEY": bool(config.higgsfield_api_key),
        "SUPABASE_URL": bool(config.supabase_url),
        "SUPABASE_KEY": bool(config.supabase_key),
    }
    missing = [k for k, v in keys.items() if not v]
    present = [k for k, v in keys.items() if v]
    parts.append("keys present: " + (", ".join(present) if present else "none"))
    if missing:
        parts.append("keys missing: " + ", ".join(missing))
    image_provider = "Higgsfield" if config.higgsfield_api_key else "Imagen 3"
    parts.append(f"image provider: {image_provider}")

    # 2. Agent initialisation — the usual culprit for "No image yet".
    agent_status: list[str] = []
    try:
        from agents.thumbnail_agent import ThumbnailAgent

        ThumbnailAgent()
        agent_status.append("thumbnail OK")
    except Exception as exc:
        agent_status.append(f"thumbnail FAILED ({str(exc)[:80]})")
    try:
        from agents.carousel_agent import CarouselAgent

        CarouselAgent()
        agent_status.append("carousel OK")
    except Exception as exc:
        agent_status.append(f"carousel FAILED ({str(exc)[:80]})")
    parts.append("; ".join(agent_status))

    # 3. Storage write test — confirms the 'media' bucket is usable.
    try:
        from core.storage import get_storage

        storage = get_storage()
        url = storage.upload(
            "diagnostics/healthcheck.txt",
            b"ok",
            content_type="text/plain",
        )
        parts.append(f"storage OK ({url.split('/')[-1]})")
    except Exception as exc:
        parts.append(f"storage FAILED ({str(exc)[:80]})")

    # 3b. END-TO-END media test — the steps the pipeline actually runs but the
    # checks above never exercised: a real Imagen generation call, Pillow slide
    # rendering, and the brand overlay. This is what pinpoints why every post
    # comes out with no image even though the agents init fine.
    media_parts: list[str] = []
    test_post = Post(
        pillar="AI Guide", platform="instagram", topic="diagnostic", title="Diagnostic"
    )

    # Imagen generation (the real API credit — often fails on free-tier keys
    # where billing isn't enabled, even though the client constructs fine).
    try:
        from agents.thumbnail_agent import ThumbnailAgent

        ta = ThumbnailAgent()
        raw = ta.generate_raw(test_post)
        provider_label = "higgsfield" if config.higgsfield_api_key else "imagen"
        media_parts.append(f"{provider_label} OK ({len(raw) // 1024}kb)")
    except Exception as exc:
        provider_label = "higgsfield" if config.higgsfield_api_key else "imagen"
        media_parts.append(f"{provider_label} FAILED ({str(exc)[:110]})")

    # Pillow slide render + brand overlay (no Imagen needed — if THIS fails,
    # even the dark-card carousel fallback produces zero slides).
    rendered_png: bytes | None = None
    try:
        from core.image_utils import add_brand_overlay, make_dark_text_card

        card = make_dark_text_card(
            "Diagnostic",
            "Render test",
            1,
            brand_name=config.brand_name,
            brand_tagline=config.brand_tagline,
        )
        rendered_png = add_brand_overlay(
            card, config.brand_name, config.brand_tagline, corner="top_right"
        )
        media_parts.append(f"render OK ({len(rendered_png) // 1024}kb)")
    except Exception as exc:
        media_parts.append(f"render FAILED ({str(exc)[:110]})")

    # 3c. PNG image upload test — the previous storage test only uploaded a
    # 2-byte text file. Actual pipeline uploads are PNG images to the
    # thumbnails/ and carousels/ paths. A mismatch in bucket policy, file size
    # limit, or MIME type would show OK above but fail here, explaining why
    # every post ends up with no image despite imagen + render passing.
    if rendered_png is not None:
        try:
            from core.storage import get_storage

            _st = get_storage()
            _st.upload("thumbnails/diagnostic-test.png", rendered_png, content_type="image/png")
            media_parts.append("thumb-upload OK")
        except Exception as exc:
            media_parts.append(f"thumb-upload FAILED ({str(exc)[:110]})")

        try:
            from core.storage import get_storage

            _st = get_storage()
            _st.upload(
                "carousels/diagnostic-test/slide_00.png",
                rendered_png,
                content_type="image/png",
            )
            media_parts.append("carousel-upload OK")
        except Exception as exc:
            media_parts.append(f"carousel-upload FAILED ({str(exc)[:110]})")

    # 3d. Carousel planning test — calls Claude to write the carousel JSON plan.
    # If this fails (bad API key, quota, JSON parse error), every carousel falls
    # back to single image silently. No Imagen credit is spent here.
    try:
        from agents.carousel_agent import CarouselAgent

        _ca = CarouselAgent()
        _plan = _ca._plan_carousel(test_post)
        media_parts.append(f"carousel-plan OK ({len(_plan.slides)} slides)")
    except Exception as exc:
        media_parts.append(f"carousel-plan FAILED ({str(exc)[:110]})")

    parts.append("; ".join(media_parts))

    # 3e. Reels self-check — the most common reason no Reels appear is that
    # video rendering fails silently on the worker (ReelsAgent swallows all
    # errors and returns None). This exercises the real render path: moviepy
    # import, ffmpeg availability, and an actual 2-frame MP4 write — so the
    # System Check shows whether Reels CAN be produced at all.
    reel_parts: list[str] = []

    # moviepy import (pure-Python wrapper around ffmpeg).
    try:
        import moviepy.editor as _mpy  # noqa: F401

        reel_parts.append("moviepy OK")
    except Exception as exc:
        reel_parts.append(f"moviepy FAILED ({str(exc)[:90]})")

    # ffmpeg binary — moviepy shells out to it for every write. It works with
    # EITHER a system ffmpeg on PATH or the static binary bundled by the
    # imageio-ffmpeg package (a moviepy dependency), so we accept both.
    import shutil as _shutil

    _ffmpeg = _shutil.which("ffmpeg")
    if not _ffmpeg:
        try:
            import imageio_ffmpeg as _iio

            _ffmpeg = _iio.get_ffmpeg_exe()
            reel_parts.append("ffmpeg OK (imageio-ffmpeg)")
        except Exception:
            reel_parts.append("ffmpeg MISSING (pip install imageio-ffmpeg)")
    else:
        reel_parts.append("ffmpeg OK (system)")

    # Freesound key presence (optional — Reels render silently without it).
    reel_parts.append(
        "freesound key set" if config.freesound_api_key else "freesound key absent (music skipped)"
    )

    # Tiny end-to-end render: 2 solid frames → MP4. Proves the exact
    # ImageClip → concatenate → write_videofile path the agent uses works.
    if _ffmpeg:
        import os as _os
        import tempfile as _tf

        _tmp_pngs: list[str] = []
        _tmp_mp4: str | None = None
        try:
            from moviepy.editor import ImageClip as _IC
            from moviepy.editor import concatenate_videoclips as _concat
            from PIL import Image as _PImage

            for _c in ((20, 20, 30), (40, 40, 60)):
                _p = _tf.NamedTemporaryFile(delete=False, suffix=".png", prefix="diag_reel_")
                _PImage.new("RGB", (1080, 1920), _c).save(_p.name)
                _p.close()
                _tmp_pngs.append(_p.name)

            _clips = [_IC(_p).set_duration(1.0) for _p in _tmp_pngs]
            _vid = _concat(_clips, method="compose")
            _out = _tf.NamedTemporaryFile(delete=False, suffix=".mp4", prefix="diag_reel_")
            _out.close()
            _tmp_mp4 = _out.name
            _vid.write_videofile(
                _tmp_mp4,
                fps=24,
                codec="libx264",
                audio=False,
                ffmpeg_params=["-pix_fmt", "yuv420p"],
                logger=None,
            )
            _vid.close()
            _kb = _os.path.getsize(_tmp_mp4) // 1024
            reel_parts.append(f"render OK ({_kb}kb mp4)")
        except Exception as exc:
            reel_parts.append(f"render FAILED ({str(exc)[:110]})")
        finally:
            for _p in [*_tmp_pngs, _tmp_mp4]:
                try:
                    if _p and _os.path.exists(_p):
                        _os.unlink(_p)
                except OSError:
                    pass

    parts.append("reels: " + "; ".join(reel_parts))

    # 3f. Meta token status — which source is in use (env vs auto-refreshed) and
    # how many days of life the long-lived token has left. Surfaces an expiry
    # countdown so the 60-day token never lapses unnoticed.
    try:
        from core.meta_token import token_info as _ti

        _info = _ti(config)
        _auto = "yes" if (config.facebook_app_id and config.facebook_app_secret) else "no"
        _days = _info.get("days_left")
        _days_str = f"{_days}d left" if _days is not None else "expiry unknown"
        parts.append(f"meta token: source={_info.get('source')}, {_days_str}, auto-refresh={_auto}")
    except Exception as exc:
        parts.append(f"meta token check error: {str(exc)[:80]}")

    # 4. Posts-table schema + what recent posts ACTUALLY saved. Selecting
    # post_type/slides also proves those columns exist — if they don't, every
    # carousel save silently fails and this read errors with the column name.
    try:
        from supabase import create_client

        sb = create_client(config.supabase_url, config.supabase_key)
        resp = (
            sb.table("posts")
            .select("platform, post_type, thumbnail_url, slides")
            .order("created_at", desc=True)
            .limit(6)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            parts.append("recent posts: none")
        else:
            summ = []
            for r in rows:
                plat = (r.get("platform") or "?")[:2]
                ptype = "C" if r.get("post_type") == "carousel" else "S"
                n = len(r.get("slides") or [])
                img = "img" if r.get("thumbnail_url") else "noimg"
                summ.append(f"{plat}:{ptype}/{n}sl/{img}")
            parts.append("recent posts " + ", ".join(summ))
    except Exception as exc:
        parts.append(f"posts schema/read FAILED ({str(exc)[:90]})")

    # 5. Facebook post-insights diagnostic — makes the exact API call that
    # analytics uses so the real Facebook error (metric deprecated, wrong
    # token scope, etc.) is visible in the System Check output on the
    # dashboard rather than buried in Railway logs.
    if config.facebook_page_id and (
        config.facebook_page_access_token or config.instagram_access_token
    ):
        try:
            import requests as _fb_req

            from agents.analytics_agent import _graph_error as _fb_err

            _fb_user_tok = config.facebook_page_access_token or config.instagram_access_token

            # Derive page token the same way the analytics agent does.
            _pt_resp = _fb_req.get(
                f"https://graph.facebook.com/v22.0/{config.facebook_page_id}",
                params={"fields": "access_token,name", "access_token": _fb_user_tok},
                timeout=8,
            )
            if _pt_resp.ok:
                _pt_data = _pt_resp.json()
                _derived_tok = _pt_data.get("access_token")
                _page_name = _pt_data.get("name", "?")[:20]
                if _derived_tok:
                    _fb_tok = _derived_tok
                    _tok_label = "page-token"
                else:
                    _fb_tok = _fb_user_tok
                    _tok_label = "user-token-fallback (pages_show_list missing?)"
            else:
                _fb_tok = _fb_user_tok
                _tok_label = f"user-token-fallback (HTTP {_pt_resp.status_code})"
                _page_name = "?"

            # Find the most recent real Facebook post to test against.
            from supabase import create_client as _sb2_cls

            _sb2 = _sb2_cls(config.supabase_url, config.supabase_key)
            _fb_rows = (
                _sb2.table("posts")
                .select("platform_post_id, published_time")
                .eq("platform", "facebook")
                .not_.is_("platform_post_id", "null")
                .neq("platform_post_id", "dry-run")
                .order("published_time", desc=True)
                .limit(1)
                .execute()
                .data
                or []
            )
            if _fb_rows:
                _fppid = _fb_rows[0].get("platform_post_id", "")
                # Mirror the analytics agent's fallback sequence. Meta deprecated
                # post_impressions* and post_views* for ALL API versions on
                # 2026-06-15. Confirmed replacement is post_media_view.
                _fb_success = False
                _fb_tried: list[str] = []
                for _mset in (
                    "post_media_view_unique,post_media_view",
                    "post_media_view",
                    "post_views_unique,post_views",
                    "post_views",
                    "post_impressions_unique,post_impressions,post_video_views",
                ):
                    try:
                        _ir = _fb_req.get(
                            f"https://graph.facebook.com/v22.0/{_fppid}/insights",
                            params={
                                "metric": _mset,
                                "period": "lifetime",
                                "access_token": _fb_tok,
                            },
                            timeout=8,
                        )
                        _ir.raise_for_status()
                        _idata = _ir.json().get("data", [])
                        if _idata:
                            _first = _idata[0]
                            _mname = _first.get("name", "?")
                            _vals = _first.get("values") or []
                            _val = _vals[0].get("value", "?") if _vals else _first.get("value", "?")
                            parts.append(
                                f"fb insights OK (page={_page_name}, tok={_tok_label}, "
                                f"{_mname}={_val}, post={_fppid[:12]})"
                            )
                            _fb_success = True
                            break
                        else:
                            _fb_tried.append(f"{_mset.split(',')[0]}: empty")
                    except Exception as _ie:
                        _fb_tried.append(f"{_mset.split(',')[0]}: {_fb_err(_ie)[:60]}")

                if not _fb_success:
                    # Discovery call — fetch the insights edge with no metric
                    # parameter. The API either returns available metrics or an
                    # error that tells us the real problem.
                    _discovery = ""
                    try:
                        _dr = _fb_req.get(
                            f"https://graph.facebook.com/v22.0/{_fppid}/insights",
                            params={"access_token": _fb_tok},
                            timeout=8,
                        )
                        _ddata = _dr.json()
                        if _dr.status_code == 200:
                            _available = [i.get("name") for i in _ddata.get("data", [])]
                            _discovery = (
                                f" available_metrics={_available}"
                                if _available
                                else " discovery=empty"
                            )
                        else:
                            _derr = _ddata.get("error", {})
                            _discovery = (
                                f" discovery_err=(#{_derr.get('code', '?')}) "
                                f"{_derr.get('message', '?')[:80]}"
                            )
                    except Exception as _de:
                        _discovery = f" discovery_err={str(_de)[:60]}"

                    parts.append(
                        f"fb insights FAIL (page={_page_name}, tok={_tok_label}, "
                        f"post={_fppid[:12]}): " + "; ".join(_fb_tried) + _discovery
                    )
            else:
                parts.append("fb insights: no published Facebook post found yet")
        except Exception as _fe:
            parts.append(f"fb insights check error: {str(_fe)[:90]}")

    report = " — ".join(parts)
    logger.info("=== Diagnostics: %s ===", report)
    return report


_last_reap_at: datetime | None = None


def run_pending_commands() -> None:
    """Execute any manual pipeline commands queued by the dashboard.

    The dashboard inserts rows into ``pipeline_commands`` with status=pending.
    This job (running every 15 s) picks them up, runs the appropriate function
    in the worker process (which has all packages installed), and marks them done.
    Unknown commands are marked failed so the dashboard can surface the error.
    """
    global _last_reap_at
    from supabase import create_client

    try:
        sb = create_client(config.supabase_url, config.supabase_key)
    except Exception:
        logger.exception("Command queue: could not connect to Supabase")
        return

    # Reap commands stuck in 'running' — a worker restart/redeploy mid-command
    # leaves the row in 'running' forever (only 'pending' rows are re-selected),
    # so the dashboard would show it as running and the outcome is lost.
    # Throttled to every 10 min so the 15-second poll stays a single SELECT.
    _reap_due = _last_reap_at is None or (datetime.now(UTC) - _last_reap_at) > timedelta(minutes=10)
    if _reap_due:
        _last_reap_at = datetime.now(UTC)
        try:
            _stale = (datetime.now(UTC) - timedelta(minutes=60)).isoformat()
            reaped = (
                sb.table("pipeline_commands")
                .update(
                    {
                        "status": "failed",
                        "finished_at": datetime.now(UTC).isoformat(),
                        "error": "worker restarted while this command was running — please retry",
                    }
                )
                .eq("status", "running")
                .lt("started_at", _stale)
                .execute()
                .data
                or []
            )
            if reaped:
                logger.warning(
                    "Command queue: reaped %d command(s) stuck in 'running'", len(reaped)
                )
        except Exception:
            logger.debug("Command queue: stuck-running reaper failed", exc_info=True)

    try:
        rows = (
            sb.table("pipeline_commands")
            .select("*")
            .eq("status", "pending")
            .order("requested_at")
            .execute()
            .data
            or []
        )
    except Exception:
        # Table may not exist yet — silently skip until the user creates it.
        logger.debug("pipeline_commands table not accessible; skipping command queue")
        return

    for row in rows:
        cmd_id = row.get("id")
        command = row.get("command", "")

        # Conditional claim: only proceed if this worker flipped pending→running.
        # During a rolling deploy two workers can both read the same pending row;
        # without this guard the command would execute twice.
        claimed = (
            sb.table("pipeline_commands")
            .update({"status": "running", "started_at": datetime.now(UTC).isoformat()})
            .eq("id", cmd_id)
            .eq("status", "pending")
            .execute()
            .data
            or []
        )
        if not claimed:
            logger.info("Command queue: '%s' (id=%s) already claimed; skipping", command, cmd_id)
            continue
        logger.info("Command queue: executing '%s' (id=%s)", command, cmd_id)

        error: str | None = None
        result_msg: str | None = None
        try:
            # pause_automation / resume_automation always execute — they ARE
            # the control plane and must never be blocked by the pause state.
            if command == "pause_automation":
                result_msg = run_pause_automation()
            elif command == "resume_automation":
                result_msg = run_resume_automation()
            elif command == "instagram_api_mode":
                result_msg = run_instagram_api_mode()
            elif command == "instagram_telegram_mode":
                result_msg = run_instagram_telegram_mode()
            elif command == "pause_content_gen":
                result_msg = run_pause_content_gen()
            elif command == "resume_content_gen":
                result_msg = run_resume_content_gen()
            elif command.startswith("pause_platform|"):
                _plat = command.split("|", 1)[1]
                result_msg = run_pause_platform(_plat)
            elif command.startswith("resume_platform|"):
                _plat = command.split("|", 1)[1]
                result_msg = run_resume_platform(_plat)
            elif command.startswith("telegram_mode|"):
                parts = command.split("|")
                if len(parts) == 3 and parts[1] in _TELEGRAM_MODE_PLATFORMS:
                    plat_arg, action = parts[1], parts[2]
                    if action == "on":
                        result_msg = run_enable_platform_telegram(plat_arg)
                    elif action == "off":
                        result_msg = run_disable_platform_telegram(plat_arg)
                    else:
                        result_msg = f"unknown telegram_mode action: {action}"
                else:
                    result_msg = f"invalid telegram_mode command: {command}"
            elif command == "publish":
                # Manual publish always runs — pause only stops scheduled jobs,
                # not explicit user actions from the dashboard.
                run_publisher(ignore_platform_pause=True)
            elif command == "all":
                result_msg = run_image_refresh()
                run_publisher(ignore_platform_pause=True)
            elif command == "diagnostics":
                # Diagnostics must work while paused — that's exactly when the
                # user needs to see what's wrong.
                result_msg = run_diagnostics()
            elif command == "refresh_token":
                # Token refresh is never blocked by pause (matches the scheduled
                # token_refresh job) so the 60-day token can't silently lapse.
                result_msg = run_token_refresh()
            elif command == "reset_stuck":
                n = _reset_stuck_publishing(threshold_minutes=0)
                result_msg = f"reset {n} stuck post(s) from 'publishing' back to 'scheduled'"
            elif _is_automation_paused():
                # All remaining commands are skipped while automation is paused.
                result_msg = "automation paused — command skipped"
                logger.info("Command queue: automation paused, skipping '%s'", command)
            elif command == "image_refresh":
                result_msg = run_image_refresh()
            elif command == "content":
                result_msg = run_content_pipeline(manual=True)
            elif command == "research":
                result_msg = run_research_pipeline()
            elif command == "weekly_strategy":
                run_weekly_strategy()
            elif command == "analytics":
                result_msg = run_analytics()
            elif command.startswith("create_infographic"):
                _parts = command.split("|", 1)
                result_msg = run_infographic_pipeline(
                    _parts[0], topic=_parts[1] if len(_parts) > 1 else None
                )
            elif command == "create_ai_news":
                result_msg = run_daily_ai_news(manual=True)
            elif command == "regen_news_bg":
                result_msg = run_regen_news_bg()
            elif command == "cleanup_hashtags":
                result_msg = run_cleanup_hashtags()
            elif command.startswith("schedule_post|"):
                result_msg = run_schedule_post(command.split("|", 1)[1])
            else:
                error = f"Unknown command: {command}"
                logger.warning("Command queue: %s", error)
        except Exception as exc:
            error = str(exc)
            if "usage limit" in error.lower():
                error = (
                    "⚠️ Anthropic API spending limit reached — "
                    "raise your cap at console.anthropic.com"
                )
            logger.exception("Command queue: '%s' failed", command)

        # The ``error`` column doubles as a free-text result message: on success
        # we store the run summary there so the dashboard can show it.
        sb.table("pipeline_commands").update(
            {
                "status": "failed" if error else "done",
                "finished_at": datetime.now(UTC).isoformat(),
                "error": error or result_msg,
            }
        ).eq("id", cmd_id).execute()
        status_str = "failed" if error else "done"
        logger.info("Command queue: '%s' finished (status=%s)", command, status_str)


def _normalize_post_hashtags(caption: str, field_tags: list) -> tuple[str, list[str], int]:
    """Return (clean_caption, hashtags[:5], original_total_count).

    Establishes a single source of truth for hashtags: any tags written inline
    in the caption are extracted and removed from the prose, then merged with
    the existing hashtags field (field tags first, inline second), de-duplicated
    case-insensitively, and capped at 5. The published caption appends the
    hashtags field, so this guarantees no post ever shows more than 5 tags.
    """
    import re

    hashtag_re = re.compile(r"(?<![\w&])#([A-Za-z0-9_]+)")
    caption = caption or ""
    inline = hashtag_re.findall(caption)

    # Strip inline hashtags from the prose and tidy the whitespace left behind.
    clean_caption = hashtag_re.sub("", caption)
    clean_caption = re.sub(r"[ \t]+", " ", clean_caption)
    clean_caption = re.sub(r" *\n", "\n", clean_caption)
    clean_caption = re.sub(r"\n{3,}", "\n\n", clean_caption).strip()

    merged: list[str] = []
    seen: set[str] = set()
    for tag in [str(t).lstrip("#").strip() for t in (field_tags or [])] + inline:
        key = tag.lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(tag)

    original_total = len(field_tags or []) + len(inline)
    return clean_caption, merged[:5], original_total


def run_cleanup_hashtags() -> str:
    """Normalise scheduled and generated posts so each has clean prose + at most 5 hashtags.

    Triggered from the dashboard. Pulls every scheduled and manual_ready post,
    extracts any hashtags written inline in the caption, merges them with the
    hashtags field, de-duplicates, caps at 5, and writes the row back only if
    anything changed. Returns a short summary for the dashboard.
    """
    from supabase import create_client

    logger.info("=== Hashtag cleanup starting ===")
    try:
        sb = create_client(config.supabase_url, config.supabase_key)
    except Exception as exc:
        logger.exception("Hashtag cleanup: could not connect to Supabase")
        return f"hashtag cleanup failed to initialise: {type(exc).__name__}: {exc}"[:300]

    try:
        rows = (
            sb.table("posts")
            .select("id, caption, hashtags")
            .in_("status", ["scheduled", "manual_ready"])
            .execute()
            .data
            or []
        )
    except Exception as exc:
        logger.exception("Hashtag cleanup: failed to read scheduled posts")
        return f"hashtag cleanup read failed: {type(exc).__name__}: {exc}"[:300]

    scanned = len(rows)
    fixed = 0
    for row in rows:
        try:
            old_caption = row.get("caption") or ""
            old_tags = list(row.get("hashtags") or [])
            new_caption, new_tags, original_total = _normalize_post_hashtags(old_caption, old_tags)
            if new_caption == old_caption and new_tags == old_tags:
                continue
            sb.table("posts").update({"caption": new_caption, "hashtags": new_tags}).eq(
                "id", row["id"]
            ).execute()
            fixed += 1
            logger.info(
                "Hashtag cleanup: post %s trimmed %d → %d tag(s)",
                row["id"][:8],
                original_total,
                len(new_tags),
            )
        except Exception:
            logger.exception("Hashtag cleanup: failed on post %s", row.get("id", "?")[:8])

    result = f"hashtag cleanup: {fixed} of {scanned} post(s) updated (scheduled + generated)"
    logger.info("=== %s ===", result)
    return result


# State-bearing commands are never pruned: the dashboard reconstructs the current
# pause/mode state from the most recent of these rows, so deleting them would make
# a pause older than 7 days silently disappear from the UI while the worker's
# Storage flag keeps everything paused.
_STATE_COMMANDS = [
    "pause_automation",
    "resume_automation",
    "pause_content_gen",
    "resume_content_gen",
    "instagram_api_mode",
    "instagram_telegram_mode",
]
_STATE_COMMAND_PREFIXES = ["pause_platform|", "resume_platform|", "telegram_mode|"]


def run_cleanup_commands() -> None:
    """Delete pipeline_commands rows older than 7 days to keep the table small.

    State-bearing commands (pause/resume, platform modes) are kept — see
    _STATE_COMMANDS above.
    """
    from supabase import create_client

    try:
        sb = create_client(config.supabase_url, config.supabase_key)
    except Exception:
        logger.exception("Cleanup: could not connect to Supabase")
        return

    try:
        cutoff = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff -= timedelta(days=7)
        query = (
            sb.table("pipeline_commands")
            .delete()
            .lt("requested_at", cutoff.isoformat())
            .not_.in_("command", _STATE_COMMANDS)
        )
        for _prefix in _STATE_COMMAND_PREFIXES:
            query = query.not_.like("command", f"{_prefix}%")
        result = query.execute()
        deleted = len(result.data) if result.data else 0
        if deleted:
            logger.info("Cleanup: deleted %d old pipeline_commands row(s)", deleted)
    except Exception:
        logger.exception("Cleanup: failed to prune pipeline_commands")


_AUTOMATION_FLAG_PATH = "config/automation.paused"
_CONTENT_GEN_FLAG_PATH = "config/content_generation.paused"
_PLATFORM_PAUSE_PREFIX = "config/platform_paused."
_PLATFORM_TELEGRAM_PREFIX = "config/telegram_mode."
_TELEGRAM_MODE_PLATFORMS = ("facebook", "twitter", "linkedin")
_PLATFORM_STATUS_PATH = "config/platform_status.json"


def write_platform_status() -> None:
    """Publish which platforms this worker can actually post to.

    The dashboard reads this file to grey out platforms that have no API
    credentials — without it the sidebar implies every platform is actively
    publishing. Written at worker startup (credentials only change with a
    redeploy) and refreshed by the diagnostics command.
    """
    import json

    from core.storage import get_storage

    try:
        payload = json.dumps(
            {
                "configured": config.configured_platforms(),
                "disabled": config.disabled_platforms,
                "dry_run": config.dry_run,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        ).encode()
        get_storage().upload(_PLATFORM_STATUS_PATH, payload, content_type="application/json")
        logger.info("Platform status written: configured=%s", config.configured_platforms())
    except Exception:
        logger.exception("Could not write platform status file")


def _is_automation_paused() -> bool:
    """Return True if the master automation kill-switch is active.

    The flag is a tiny file in Supabase Storage at _AUTOMATION_FLAG_PATH.
    Presence = paused; absence (404 / None) = running. Fail-open: if
    Storage is unreachable we assume running so a connectivity blip never
    permanently stalls the worker.
    """
    try:
        from core.storage import get_storage

        return get_storage().download(_AUTOMATION_FLAG_PATH) is not None
    except Exception:
        return False


def run_pause_automation() -> str:
    """Activate the master kill-switch — all pipeline jobs will skip."""
    from core.storage import get_storage

    logger.warning("=== AUTOMATION PAUSED by dashboard command ===")
    try:
        get_storage().upload(_AUTOMATION_FLAG_PATH, b"paused", content_type="text/plain")
    except Exception as exc:
        logger.exception("Could not write automation pause flag")
        return f"pause flag write failed: {type(exc).__name__}: {exc}"[:300]
    return "automation paused — all pipeline jobs will skip until resumed"


def run_resume_automation() -> str:
    """Deactivate the master kill-switch — all pipeline jobs resume normally."""
    from core.storage import get_storage

    logger.warning("=== AUTOMATION RESUMED by dashboard command ===")
    try:
        ok = get_storage().delete(_AUTOMATION_FLAG_PATH)
    except Exception as exc:
        logger.exception("Could not delete automation pause flag")
        raise RuntimeError(f"resume failed — storage raised {type(exc).__name__}: {exc}") from exc
    if not ok:
        msg = "resume failed — could not delete pause flag from Storage; check worker logs"
        logger.error(msg)
        raise RuntimeError(msg)
    return "automation resumed — all pipeline jobs running normally"


def _is_content_gen_paused() -> bool:
    """Return True if automated content generation jobs are paused.

    Fail-open: if Storage is unreachable we assume running.
    """
    try:
        from core.storage import get_storage

        return get_storage().download(_CONTENT_GEN_FLAG_PATH) is not None
    except Exception:
        return False


def _is_platform_paused(platform: str) -> bool:
    """Return True if automatic scheduled publishing is paused for *platform*.

    Fail-open: if Storage is unreachable we assume running.
    Manual 'Publish Now' calls bypass this check via ignore_platform_pause.
    """
    try:
        from core.storage import get_storage

        return get_storage().download(f"{_PLATFORM_PAUSE_PREFIX}{platform}") is not None
    except Exception:
        return False


def _content_gen_guard(fn):
    """Wrap a content-creation job so it skips while content generation is paused."""
    from functools import wraps

    @wraps(fn)
    def _wrapper(*args, **kwargs):
        if _is_content_gen_paused():
            logger.info("Content generation paused — skipping scheduled job %s", fn.__name__)
            return None
        return fn(*args, **kwargs)

    return _wrapper


def run_pause_content_gen() -> str:
    """Pause automated content creation — publishing continues unaffected."""
    from core.storage import get_storage

    logger.warning("=== CONTENT GENERATION PAUSED by dashboard command ===")
    try:
        get_storage().upload(_CONTENT_GEN_FLAG_PATH, b"paused", content_type="text/plain")
    except Exception as exc:
        logger.exception("Could not write content generation pause flag")
        return f"content gen pause flag write failed: {type(exc).__name__}: {exc}"[:300]
    return "content generation paused — publishing continues; manual dashboard buttons still work"


def run_resume_content_gen() -> str:
    """Resume automated content creation."""
    from core.storage import get_storage

    logger.warning("=== CONTENT GENERATION RESUMED by dashboard command ===")
    try:
        ok = get_storage().delete(_CONTENT_GEN_FLAG_PATH)
    except Exception as exc:
        logger.exception("Could not delete content generation pause flag")
        raise RuntimeError(f"resume failed — storage raised {type(exc).__name__}: {exc}") from exc
    if not ok:
        msg = "resume failed — could not delete content gen pause flag; check worker logs"
        logger.error(msg)
        raise RuntimeError(msg)
    return "content generation resumed — automated research and creation running normally"


def run_pause_platform(platform: str) -> str:
    """Pause automatic scheduled publishing for *platform*.

    Manual 'Publish Now' from the dashboard always bypasses this.
    """
    from core.storage import get_storage

    logger.warning("=== PLATFORM %s PUBLISHING PAUSED by dashboard command ===", platform)
    try:
        get_storage().upload(
            f"{_PLATFORM_PAUSE_PREFIX}{platform}", b"paused", content_type="text/plain"
        )
    except Exception as exc:
        logger.exception("Could not write platform pause flag for %s", platform)
        return f"platform pause flag write failed: {type(exc).__name__}: {exc}"[:300]
    return f"{platform} automatic publishing paused — manual Publish Now still works"


def run_resume_platform(platform: str) -> str:
    """Resume automatic scheduled publishing for *platform*."""
    from core.storage import get_storage

    logger.warning("=== PLATFORM %s PUBLISHING RESUMED by dashboard command ===", platform)
    try:
        ok = get_storage().delete(f"{_PLATFORM_PAUSE_PREFIX}{platform}")
    except Exception as exc:
        logger.exception("Could not delete platform pause flag for %s", platform)
        raise RuntimeError(f"resume failed — storage raised {type(exc).__name__}: {exc}") from exc
    if not ok:
        msg = (
            f"resume failed — could not delete platform pause flag for {platform};"
            " check worker logs"
        )
        logger.error(msg)
        raise RuntimeError(msg)
    return f"{platform} automatic publishing resumed — scheduled posts will go out normally"


_INSTAGRAM_API_FLAG_PATH = "config/instagram.api_mode"


def _is_instagram_api_mode() -> bool:
    """Return True if Instagram posts should publish via the Graph API.

    Default (flag absent) = Telegram mode. Fail-open: if Storage is
    unreachable we assume Telegram mode so posts aren't accidentally
    published via the API against the user's preference.
    """
    try:
        from core.storage import get_storage

        return get_storage().download(_INSTAGRAM_API_FLAG_PATH) is not None
    except Exception:
        return False


def run_instagram_api_mode() -> str:
    """Switch Instagram publishing to the Graph API."""
    from core.storage import get_storage

    logger.warning("=== INSTAGRAM switching to Graph API publish mode ===")
    try:
        get_storage().upload(_INSTAGRAM_API_FLAG_PATH, b"api_mode", content_type="text/plain")
    except Exception as exc:
        logger.exception("Could not write instagram api_mode flag")
        return f"instagram api mode flag write failed: {type(exc).__name__}: {exc}"[:300]
    return "instagram api mode enabled — posts will publish automatically via Graph API"


def run_instagram_telegram_mode() -> str:
    """Switch Instagram publishing back to Telegram (manual native posting)."""
    from core.storage import get_storage

    logger.warning("=== INSTAGRAM switching back to Telegram mode ===")
    try:
        get_storage().delete(_INSTAGRAM_API_FLAG_PATH)
    except Exception as exc:
        logger.exception("Could not delete instagram api_mode flag")
        return f"instagram telegram mode flag delete failed: {type(exc).__name__}: {exc}"[:300]
    return "instagram telegram mode restored — posts will be sent to Telegram for manual posting"


def run_enable_platform_telegram(platform: str) -> str:
    """Enable Telegram-delivery mode for *platform* (posts created from now → Telegram)."""
    from core.storage import get_storage

    logger.warning("=== %s switching to Telegram delivery mode ===", platform.upper())
    path = f"{_PLATFORM_TELEGRAM_PREFIX}{platform}"
    ts = datetime.now(UTC).isoformat()
    try:
        get_storage().upload(path, ts.encode(), content_type="text/plain")
    except Exception as exc:
        logger.exception("Could not write telegram_mode flag for %s", platform)
        return f"telegram mode flag write failed for {platform}: {type(exc).__name__}: {exc}"[:300]
    return f"{platform} telegram mode enabled — future posts will be sent to Telegram"


def run_disable_platform_telegram(platform: str) -> str:
    """Disable Telegram-delivery mode for *platform* (revert to direct API publishing)."""
    from core.storage import get_storage

    logger.warning("=== %s switching back to direct publishing ===", platform.upper())
    path = f"{_PLATFORM_TELEGRAM_PREFIX}{platform}"
    try:
        get_storage().delete(path)
    except Exception as exc:
        logger.exception("Could not delete telegram_mode flag for %s", platform)
        return f"telegram mode flag delete failed for {platform}: {type(exc).__name__}: {exc}"[:300]
    return f"{platform} telegram mode disabled — posts will publish directly"


def run_infographic_pipeline(
    command: str = "create_infographic",
    topic: str | None = None,
    manual: bool = True,
) -> str:
    """Generate a data-driven infographic Reel and schedule it for publishing.

    *command* encodes the target platform(s):
      "create_infographic"          → Instagram Reel + Facebook Reel (default)
      "create_infographic_ig"       → Instagram Reel only
      "create_infographic_fb"       → Facebook Reel only
      "create_infographic_static"   → Static image post on Instagram
    *topic* overrides the agent's automatic daily-rotation topic selection.
    *manual* — when True (default, since infographics are always dashboard-triggered),
    posts land in ``manual_ready`` status for the Generated tab rather than being
    auto-scheduled immediately.
    """
    from agents.infographic_agent import InfographicAgent
    from agents.scheduler_agent import SchedulerAgent

    logger.info("=== Infographic pipeline starting (command=%s) ===", command)

    platform_map = {
        "create_infographic_ig": [Platform.INSTAGRAM.value],
        "create_infographic_fb": [Platform.FACEBOOK.value],
        "create_infographic_static": [Platform.INSTAGRAM.value],
        "create_infographic_wheel": [Platform.INSTAGRAM.value],
        "create_infographic_dark": [Platform.INSTAGRAM.value],
        "create_infographic_light": [Platform.INSTAGRAM.value],
        "create_infographic_rich_dark": [Platform.INSTAGRAM.value, Platform.FACEBOOK.value],
        "create_infographic_rich_light": [Platform.INSTAGRAM.value, Platform.FACEBOOK.value],
    }
    platforms = platform_map.get(command, None)  # None → agent picks defaults
    fmt_map = {
        "create_infographic_static": "static",
        "create_infographic_wheel": "wheel",
        "create_infographic_dark": "dark",
        "create_infographic_light": "light",
        "create_infographic_rich_dark": "rich_dark",
        "create_infographic_rich_light": "rich_light",
    }
    fmt = fmt_map.get(command, "reel")

    try:
        agent = InfographicAgent()
        scheduler_agent = SchedulerAgent()
        db = get_database()
    except Exception as exc:
        logger.exception("Infographic pipeline: could not initialise")
        return f"infographic pipeline failed to initialise: {type(exc).__name__}: {exc}"[:300]

    if topic:
        logger.info("Infographic pipeline: using requested topic=%r", topic)
    try:
        posts = agent.create_posts(topic=topic, platforms=platforms, fmt=fmt)
    except Exception as exc:
        msg = str(exc)
        logger.exception("Infographic pipeline: create_posts failed")
        if "usage limit" in msg.lower():
            return (
                "⚠️ Anthropic API spending limit reached — raise your cap at console.anthropic.com"
            )
        return f"infographic creation failed: {type(exc).__name__}: {exc}"[:300]

    if not posts:
        return "infographic pipeline: no posts created"

    last_slot: dict[str, datetime] = db.latest_scheduled_time_by_platform()
    created = 0
    for post in posts:
        try:
            if manual:
                post.mark(PostStatus.MANUAL_READY)
                db.insert(post)
                logger.info(
                    "Infographic pipeline: %s post %s → Generated tab (manual_ready)",
                    post.platform,
                    post.id,
                )
            else:
                scheduler_agent.schedule(post, after=last_slot.get(post.platform))
                last_slot[post.platform] = post.scheduled_time
                db.insert(post)
                logger.info(
                    "Infographic pipeline: scheduled %s post %s at %s",
                    post.platform,
                    post.id,
                    post.scheduled_time,
                )
            created += 1
        except Exception:
            logger.exception("Infographic pipeline: failed to persist post %s", post.id)

    dest = "Generated tab" if manual else "Scheduled"
    msg = f"infographic: created {created} post(s) → {dest}"
    logger.info("=== Infographic pipeline finished: %s ===", msg)
    return msg


def run_schedule_post(post_id: str) -> str:
    """Schedule a single ``manual_ready`` post using the auto-scheduler.

    Called when the user clicks "Add to Schedule" in the Generated tab.
    Finds the next optimal slot after everything already queued and
    transitions the post from ``manual_ready`` → ``scheduled``.
    """
    from agents.scheduler_agent import SchedulerAgent

    logger.info("Scheduling manual post %s", post_id)
    db = get_database()
    try:
        post = db.get(post_id)
        if not post:
            return f"post {post_id} not found"
        scheduler_agent = SchedulerAgent()
        last_slot = db.latest_scheduled_time_by_platform()
        scheduler_agent.schedule(post, after=last_slot.get(post.platform))
        db.upsert(post)
        ts = post.scheduled_time.strftime("%a %d %b %H:%M") if post.scheduled_time else "unknown"
        logger.info("Post %s scheduled for %s", post_id, ts)
        return f"scheduled for {ts}"
    except Exception as exc:
        logger.exception("Failed to schedule post %s", post_id)
        return f"scheduling failed: {exc}"


def run_token_refresh() -> str:
    """Refresh the Meta long-lived user token before its ~60-day expiry.

    Re-exchanges the current token for a fresh one and stores it in Supabase so
    the publisher/analytics pick it up without a redeploy. Skips the network
    call while the token still has comfortable headroom. Safe to run weekly.
    """
    from core.meta_token import refresh_user_token, token_info

    logger.info("=== Meta token refresh starting ===")
    try:
        changed, msg = refresh_user_token(config)
    except Exception as exc:
        logger.exception("Meta token refresh failed")
        return f"token refresh failed: {type(exc).__name__}: {exc}"[:300]

    info = token_info(config)
    days_left = info.get("days_left")
    if days_left is not None and days_left <= 14 and not changed:
        logger.warning(
            "Meta token has only %s day(s) left and was not refreshed: %s", days_left, msg
        )
    logger.info("=== Meta token refresh: %s ===", msg)
    return msg


def run_regen_news_bg() -> str:
    """Delete the cached news carousel background and immediately regenerate it via AI.

    Deletes ``templates/news_carousel_bg.png`` from Supabase Storage, then calls
    Higgsfield (Imagen fallback) to produce a fresh abstract Brite Blue background,
    bakes the readability overlay, and stores the result as the new template.
    Returns a detailed status string surfaced on the dashboard so the user can see
    whether the AI generation succeeded or why it failed.
    """
    from agents.news_agent import _NEWS_BG_PROMPT, NewsAgent
    from core.image_utils import make_news_bg_template
    from core.storage import get_storage

    path = NewsAgent._BG_TEMPLATE_PATH
    logger.info("=== Regenerate news background: deleting %s ===", path)

    try:
        storage = get_storage()
    except Exception as exc:
        logger.exception("Regenerate news background: storage unavailable")
        return f"background reset failed: storage unavailable: {exc}"[:300]

    storage.delete(path)  # best-effort; missing template is fine

    # Immediately generate a fresh AI background so the result is visible right away.
    logger.info("Regenerate news background: generating fresh AI image")
    ai_base: bytes | None = None
    source = ""
    fail_reason = ""

    try:
        from agents.infographic_agent import InfographicAgent

        ia = InfographicAgent(config)
    except Exception as exc:
        fail_reason = f"could not init image agent: {exc}"
        logger.warning("Regenerate news background: %s", fail_reason)
        ia = None  # type: ignore[assignment]

    if ia is not None:
        if config.higgsfield_api_key:
            try:
                logger.info("Regenerate news background: trying Higgsfield (1:1)")
                ai_base = ia._higgsfield_background(aspect_ratio="1:1", prompt=_NEWS_BG_PROMPT)
                source = "Higgsfield"
            except Exception as exc:
                fail_reason = f"Higgsfield: {exc}"
                logger.warning("Regenerate news background: Higgsfield failed: %s", exc)

        if ai_base is None and config.google_api_key:
            try:
                logger.info("Regenerate news background: trying Imagen (1:1)")
                ai_base = ia._imagen_background(aspect_ratio="1:1", prompt=_NEWS_BG_PROMPT)
                source = "Imagen"
            except Exception as exc:
                fail_reason = f"Higgsfield+Imagen both failed; last error: {exc}"
                logger.warning("Regenerate news background: Imagen also failed: %s", exc)

    if ai_base is None:
        if not (config.higgsfield_api_key or config.google_api_key):
            fail_reason = "no image API key configured (HIGGSFIELD_API_KEY / GOOGLE_API_KEY)"
        return (
            f"template cleared — AI generation failed ({fail_reason or 'unknown reason'}). "
            "Check Railway logs. A plain gradient will be used until a key is configured."
        )

    try:
        template = make_news_bg_template(base_bytes=ai_base)
    except Exception as exc:
        logger.exception("Regenerate news background: bake failed")
        return f"AI image generated via {source} but bake failed: {exc}"[:300]

    try:
        storage.upload(path, template, content_type="image/png")
    except Exception as exc:
        logger.exception("Regenerate news background: upload failed")
        return f"AI background generated via {source} but upload failed: {exc}"[:300]

    msg = (
        f"new AI background generated via {source} ({len(template) // 1024} kb) "
        "and stored — next carousel run will use it"
    )
    logger.info("=== Regenerate news background: %s ===", msg)
    return msg


def run_daily_ai_news(manual: bool = False) -> str:
    """Fetch today's top AI news and create a carousel for IG + FB.

    When *manual* is True (dashboard-triggered), posts land in ``manual_ready``
    status (Generated tab) for review before publishing. When False (noon cron),
    posts are auto-scheduled for immediate publication.
    """
    from agents.news_agent import NewsAgent

    logger.info("=== Daily AI news carousel starting (manual=%s) ===", manual)
    try:
        agent = NewsAgent()
        db = get_database()
    except Exception as exc:
        logger.exception("Daily AI news: failed to initialise")
        return f"daily AI news failed to initialise: {type(exc).__name__}: {exc}"[:300]

    try:
        posts = agent.create_news_carousel()
    except Exception as exc:
        logger.exception("Daily AI news: create_news_carousel failed")
        return f"daily AI news creation failed: {type(exc).__name__}: {exc}"[:300]

    if not posts:
        return "daily AI news: no posts created"

    now = datetime.now(UTC)
    created = 0
    for post in posts:
        try:
            if manual:
                post.mark(PostStatus.MANUAL_READY)
            else:
                # Publish immediately — news is time-sensitive.
                # Set scheduled_time to now so the publisher picks it up on its
                # next 5-minute cycle rather than queuing it behind other posts.
                post.scheduled_time = now
                post.mark(PostStatus.SCHEDULED)
            db.insert(post)
            logger.info(
                "Daily AI news: %s %s post %s",
                "queued manual" if manual else "publishing now",
                post.platform,
                post.id,
            )
            created += 1
        except Exception:
            logger.exception("Daily AI news: failed to persist post %s", post.id)

    if manual:
        result = f"daily AI news: {created} post(s) → Generated tab"
    else:
        result = f"daily AI news: {created} post(s) publishing now"
    logger.info("=== Daily AI news finished: %s ===", result)
    return result


def run_weekly_strategy() -> None:
    """Monday competitor-research pass: queue 7 shaped topic ideas for approval.

    Studies what's working in the niche and (optionally) on competitor accounts
    set via COMPETITOR_URLS in Railway.  Generates 7 original post ideas shaped
    by proven viral patterns and drops them into the approval queue — same gate
    as the daily research pipeline so nothing posts without your sign-off.

    Runs every Monday at 07:00 (after the 05:30 daily research run).
    """
    from agents.content_agent import ContentAgent
    from agents.research_agent import ResearchAgent

    logger.info("=== Weekly strategy pipeline starting ===")
    try:
        db = get_database()
        content_agent = ContentAgent()
        research_agent = ResearchAgent(content_agent=content_agent, db=db)
    except Exception:
        logger.exception("Weekly strategy pipeline could not initialise; skipping")
        return

    try:
        topics = research_agent.weekly_strategy(
            competitor_urls=config.competitor_urls,
            target_count=7,
        )
    except Exception:
        logger.exception("Weekly strategy research failed; skipping")
        return

    logger.info("=== Weekly strategy finished: %d topic(s) queued for approval ===", len(topics))


def _safe_init(agent_cls, label: str):
    """Instantiate an agent, returning None if it's unconfigured."""
    try:
        return agent_cls()
    except Exception as exc:
        logger.warning("%s agent unavailable: %s", label, exc)
        return None


def _append_error(post: Post, msg: str) -> None:
    """Append a short error note to post.error (separator ' | ')."""
    if post.error:
        post.error = f"{post.error} | {msg}"[:1000]
    else:
        post.error = msg[:1000]


def _paused_guard(fn):
    """Wrap a scheduled job so it silently skips while automation is paused."""
    from functools import wraps

    @wraps(fn)
    def _wrapper(*args, **kwargs):
        if _is_automation_paused():
            logger.info("Automation paused — skipping scheduled job %s", fn.__name__)
            return None
        return fn(*args, **kwargs)

    return _wrapper


def build_scheduler():
    """Wire up the recurring jobs."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler(timezone=config.timezone)

    scheduler.add_job(
        _paused_guard(_content_gen_guard(run_research_pipeline)),
        trigger=CronTrigger(hour=5, minute=30, timezone=config.timezone),
        id="research_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _paused_guard(_content_gen_guard(run_approved_pipeline)),
        trigger=IntervalTrigger(minutes=15),
        id="approved_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        _paused_guard(_content_gen_guard(run_content_pipeline)),
        trigger=CronTrigger(hour=6, minute=0, timezone=config.timezone),
        id="content_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _paused_guard(run_publisher),
        trigger=IntervalTrigger(minutes=5),
        id="publisher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        _paused_guard(run_qc_retry),
        trigger=IntervalTrigger(hours=4),
        id="qc_retry",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        _paused_guard(run_image_refresh),
        trigger=CronTrigger(hour=2, minute=0, timezone=config.timezone),
        id="image_refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _paused_guard(run_analytics),
        trigger=IntervalTrigger(hours=2),
        id="analytics",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # run_pending_commands is NOT guarded — it IS the control plane.
    # It processes pause_automation / resume_automation commands; guarding it
    # would make the system impossible to resume without a Railway redeploy.
    # 15-second poll keeps dashboard buttons feeling responsive; each empty
    # poll is one tiny SELECT (Supabase has no per-request charge).
    scheduler.add_job(
        run_pending_commands,
        trigger=IntervalTrigger(seconds=15),
        id="command_queue",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    # Maintenance jobs — no API cost, always run regardless of pause state.
    scheduler.add_job(
        run_cleanup_commands,
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=config.timezone),
        id="cleanup_commands",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _paused_guard(_content_gen_guard(run_weekly_strategy)),
        trigger=CronTrigger(day_of_week="mon", hour=7, minute=0, timezone=config.timezone),
        id="weekly_strategy",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Token refresh: maintenance, no content cost. Left unguarded so the
    # 60-day token never silently lapses while automation is paused.
    scheduler.add_job(
        run_token_refresh,
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=config.timezone),
        id="token_refresh",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Daily AI/tech infographic Reel — researched, designed and scheduled each
    # morning after the research pipeline (05:30) and content pipeline (06:00)
    # have already run so the infographic slot falls after regular posts.
    scheduler.add_job(
        _paused_guard(_content_gen_guard(run_infographic_pipeline)),
        trigger=CronTrigger(hour=11, minute=0, timezone=config.timezone),
        id="infographic_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Daily AI news carousel — fetches today's top 3 AI stories via web search
    # and publishes a branded 5-slide carousel to Instagram + Facebook at noon.
    scheduler.add_job(
        _paused_guard(_content_gen_guard(run_daily_ai_news)),
        trigger=CronTrigger(hour=12, minute=0, timezone=config.timezone),
        id="daily_ai_news",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    return scheduler


def main() -> None:
    configure_logging(config.log_level)
    if config.dry_run:
        logger.warning(
            "DRY_RUN=true — no posts will be published. "
            "Set DRY_RUN=false in Railway env vars to go live."
        )
    else:
        logger.warning("DRY_RUN=false — posts WILL be published to live platforms!")
    logger.info(
        "Starting %s automation worker (tz=%s, dry_run=%s)",
        config.brand_name,
        config.timezone,
        config.dry_run,
    )

    # Tell the dashboard which platforms this worker can actually post to.
    write_platform_status()

    scheduler = build_scheduler()

    def _shutdown(signum, _frame):
        logger.info("Received signal %s; shutting down scheduler", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
