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
from datetime import UTC, datetime
from itertools import cycle

from core.config import config, configure_logging
from core.database import get_database
from core.models import Platform, Post, PostStatus

logger = logging.getLogger("scheduler.cron")

# Platforms that need a video asset before publishing.
_VIDEO_PLATFORMS = {Platform.YOUTUBE.value, Platform.TIKTOK.value}


def _round_robin_platforms(pillars: list[str], platforms: list[str]) -> list[tuple]:
    """Pair each pillar with a platform, rotating through the platforms."""
    if not platforms:
        return []
    wheel = cycle(platforms)
    return [(pillar, next(wheel)) for pillar in pillars]


def run_content_pipeline() -> None:
    """Generate, enrich, and schedule a batch of posts."""
    from agents.content_agent import ContentAgent
    from agents.quality_agent import QualityAgent, QualityError
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
        return

    thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
    video_agent = _safe_init(VideoAgent, "video")
    quality_agent = _safe_init(QualityAgent, "quality")

    platforms = config.configured_platforms() or config.platforms
    pairings = _round_robin_platforms(config.content_pillars, platforms)

    created = 0
    for pillar, platform in pairings[: config.posts_per_run] or pairings:
        post = Post(pillar=pillar, platform=platform)
        try:
            db.insert(post)
            content_agent.generate(post)
            if quality_agent:
                quality_agent.fix_text(post)
            try:
                _generate_media(post, thumbnail_agent, video_agent, quality_agent)
            except QualityError as exc:
                logger.warning("QC rejected post %s: %s", post.id, exc)
                db.update_status(post, PostStatus.FAILED, error=f"QC: {exc}")
                continue
            scheduler_agent.schedule(post)
            db.upsert(post)
            created += 1
        except Exception:
            logger.exception("Failed building post for %s/%s", pillar, platform)
            try:
                db.update_status(post, PostStatus.FAILED, error="pipeline error")
            except Exception:
                logger.exception("Could not persist failure for post %s", post.id)

    logger.info("=== Content pipeline finished: %d post(s) scheduled ===", created)


def run_research_pipeline() -> None:
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
        return

    try:
        posts = research_agent.run()
    except Exception:
        logger.exception("Research pipeline failed during discovery; skipping run")
        return

    scheduled = _finalise_posts(posts, db, scheduler_agent)
    logger.info("=== Research pipeline finished: %d post(s) scheduled ===", scheduled)


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
    scheduled = _finalise_posts(posts, db, scheduler_agent, persist_insert=False)
    logger.info("Approved-topic pipeline finished: %d post(s) scheduled", scheduled)


def _finalise_posts(posts: list[Post], db, scheduler_agent, *, persist_insert: bool = True) -> int:
    """Add media, pick a slot, and persist each post; return the count scheduled.

    Failure-isolated per post — one failing post is marked failed and the
    rest continue. Set persist_insert=False when the posts are already in the
    database (e.g. inserted by generate_for_approved).
    """
    from agents.carousel_agent import CarouselAgent
    from agents.quality_agent import QualityAgent, QualityError
    from agents.thumbnail_agent import ThumbnailAgent
    from agents.video_agent import VideoAgent

    thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
    video_agent = _safe_init(VideoAgent, "video")
    quality_agent = _safe_init(QualityAgent, "quality")

    last_slot: dict[str, datetime] = {}

    carousel_agent = _safe_init(CarouselAgent, "carousel")
    configured_platforms = set(config.configured_platforms())

    scheduled = 0
    for post in posts:
        try:
            if quality_agent:
                quality_agent.fix_text(post)
            try:
                _generate_media(post, thumbnail_agent, video_agent, quality_agent)
            except QualityError as exc:
                logger.warning("QC rejected post %s: %s", post.id, exc)
                post.mark(PostStatus.FAILED, error=f"QC: {exc}")
                if persist_insert:
                    db.insert(post)
                else:
                    db.upsert(post)
                continue
            after = last_slot.get(post.platform)
            scheduler_agent.schedule(post, after=after)
            last_slot[post.platform] = post.scheduled_time
            if persist_insert:
                db.insert(post)
            else:
                db.upsert(post)
            scheduled += 1

            if carousel_agent and post.platform in (
                Platform.INSTAGRAM.value,
                Platform.FACEBOOK.value,
            ):
                try:
                    carousel = carousel_agent.create_from_post(post, quality_agent=quality_agent)
                    carousel_after = last_slot.get(post.platform)
                    scheduler_agent.schedule(carousel, after=carousel_after)
                    last_slot[post.platform] = carousel.scheduled_time
                    db.insert(carousel)
                    scheduled += 1
                    logger.info("Created carousel variant for post %s", post.id)

                    if (
                        post.platform == Platform.INSTAGRAM.value
                        and Platform.FACEBOOK.value in configured_platforms
                    ):
                        fb_carousel = Post(
                            pillar=carousel.pillar,
                            platform=Platform.FACEBOOK.value,
                            topic=carousel.topic,
                            caption=carousel.caption,
                            hashtags=list(carousel.hashtags),
                            title=carousel.title,
                            thumbnail_url=carousel.thumbnail_url,
                            post_type="carousel",
                            slides=list(carousel.slides),
                        )
                        fb_after = last_slot.get(Platform.FACEBOOK.value)
                        scheduler_agent.schedule(fb_carousel, after=fb_after)
                        last_slot[Platform.FACEBOOK.value] = fb_carousel.scheduled_time
                        db.insert(fb_carousel)
                        scheduled += 1
                except Exception:
                    logger.exception("Carousel creation failed for post %s; continuing", post.id)

        except Exception:
            logger.exception("Failed finalising post %s", post.id)
            try:
                db.update_status(post, PostStatus.FAILED, error="finalise error")
            except Exception:
                logger.exception("Could not persist failure for post %s", post.id)
    return scheduled


def _generate_media(post: Post, thumbnail_agent, video_agent, quality_agent=None) -> None:
    """Generate media assets for *post*.

    Thumbnail: Imagen is called once. If a quality_agent is provided the
    overlay is QC-checked in memory before uploading — re-applying the
    overlay (free Pillow) once if it fails — so no Imagen credit is ever
    wasted on a branding problem.  Raises QualityError if both overlay
    attempts fail; the caller should mark the post failed.

    Video: best-effort, never raises.
    """
    from agents.quality_agent import QualityError

    needs_video = post.platform in _VIDEO_PLATFORMS

    if thumbnail_agent is not None:
        try:
            raw_bytes = thumbnail_agent.generate_raw(post)
        except Exception:
            logger.exception("Imagen generation failed for post %s", post.id)
            raw_bytes = None

        if raw_bytes is not None:
            final_bytes = thumbnail_agent.apply_overlay(raw_bytes)

            if quality_agent is not None:
                try:
                    quality_agent.check_image_bytes(post, final_bytes)
                except QualityError as exc:
                    logger.warning(
                        "QC: overlay failed for post %s: %s — re-applying (no new Imagen call)",
                        post.id,
                        exc,
                    )
                    final_bytes = thumbnail_agent.apply_overlay(raw_bytes)
                    # Raises QualityError if still wrong — caller handles it.
                    quality_agent.check_image_bytes(post, final_bytes)

            try:
                thumbnail_agent.upload(post, final_bytes)
            except Exception:
                logger.exception("Thumbnail upload failed for post %s", post.id)

    if needs_video and video_agent is not None:
        try:
            video_agent.generate(post)
        except Exception:
            logger.exception("Video generation failed for post %s", post.id)


def run_image_refresh() -> None:
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

        def _carousel_rows(status: str) -> list:
            return (
                sb.table("posts")
                .select("*")
                .eq("post_type", "carousel")
                .eq("status", status)
                .is_("thumbnail_url", "null")
                .execute()
                .data
                or []
            )

        def _standard_rows(status: str) -> list:
            return (
                sb.table("posts")
                .select("*")
                .eq("post_type", "standard")
                .eq("status", status)
                .is_("thumbnail_url", "null")
                .execute()
                .data
                or []
            )

        # --- Carousels (scheduled + failed with no image) ---
        if carousel_agent:
            for status in ("scheduled", "failed"):
                for c in _carousel_rows(status):
                    try:
                        source = Post(
                            id=c["id"],
                            pillar=c["pillar"],
                            platform=c["platform"],
                            topic=c.get("topic", ""),
                            caption=c.get("caption", ""),
                            hashtags=list(c.get("hashtags") or []),
                        )
                        new_id = str(_uuid.uuid4())
                        plan = carousel_agent._plan_carousel(source)
                        slides = carousel_agent._generate_images(new_id, source, plan)
                        update: dict = {
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

        # --- Standard posts (scheduled + failed with no image) ---
        if thumbnail_agent:
            for status in ("scheduled", "failed"):
                for p in _standard_rows(status):
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

        logger.info("=== Image refresh finished: %d asset(s) updated ===", refreshed)
    except Exception:
        logger.exception("Image refresh could not initialise; skipping run")


def run_publisher() -> None:
    """Publish every post whose scheduled time has arrived."""
    from agents.publisher_agent import PublisherAgent

    try:
        db = get_database()
        publisher = PublisherAgent()
    except Exception:
        logger.exception("Publisher could not initialise; skipping run")
        return

    due = db.due_for_publishing(now=datetime.now(UTC))
    if not due:
        logger.debug("No posts due for publishing")
        return

    logger.info("Publishing %d due post(s)", len(due))
    for post in due:
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
            try:
                db.upsert(post)
            except Exception:
                logger.exception("Could not persist post %s after publish", post.id)


def run_qc_retry() -> None:
    """Regenerate thumbnails and re-run QC for posts that failed the image check.

    Runs hourly. Finds posts whose error field starts with 'QC:', regenerates
    the thumbnail, and reschedules any that pass the second check. Posts that
    fail again are left as failed with an updated error message.
    """
    from supabase import create_client

    from agents.quality_agent import QualityAgent, QualityError
    from agents.scheduler_agent import SchedulerAgent
    from agents.thumbnail_agent import ThumbnailAgent

    try:
        sb = create_client(config.supabase_url, config.supabase_key)
        thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
        quality_agent = _safe_init(QualityAgent, "quality")
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
            try:
                _generate_media(
                    post, thumbnail_agent, video_agent=None, quality_agent=quality_agent
                )
            except QualityError as exc:
                logger.warning("QC retry: post %s still failing after regen: %s", post.id[:8], exc)
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


def _safe_init(agent_cls, label: str):
    """Instantiate an agent, returning None if it's unconfigured."""
    try:
        return agent_cls()
    except Exception as exc:
        logger.warning("%s agent unavailable: %s", label, exc)
        return None


def build_scheduler():
    """Wire up the recurring jobs."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler(timezone=config.timezone)

    scheduler.add_job(
        run_research_pipeline,
        trigger=CronTrigger(hour=5, minute=30, timezone=config.timezone),
        id="research_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        run_approved_pipeline,
        trigger=IntervalTrigger(minutes=15),
        id="approved_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        run_content_pipeline,
        trigger=CronTrigger(hour=6, minute=0, timezone=config.timezone),
        id="content_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        run_publisher,
        trigger=IntervalTrigger(minutes=5),
        id="publisher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        run_qc_retry,
        trigger=IntervalTrigger(hours=1),
        id="qc_retry",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    scheduler.add_job(
        run_image_refresh,
        trigger=CronTrigger(hour=2, minute=0, timezone=config.timezone),
        id="image_refresh",
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
