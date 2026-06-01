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
"""

from __future__ import annotations

import logging
import signal
import sys
from datetime import UTC, datetime
from itertools import cycle

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agents.content_agent import ContentAgent
from agents.publisher_agent import PublisherAgent
from agents.research_agent import ResearchAgent
from agents.scheduler_agent import SchedulerAgent
from agents.thumbnail_agent import ThumbnailAgent
from agents.video_agent import VideoAgent
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
    logger.info("=== Content pipeline starting ===")
    try:
        db = get_database()
        content_agent = ContentAgent()
        scheduler_agent = SchedulerAgent()
    except Exception:
        logger.exception("Content pipeline could not initialise; skipping run")
        return

    # Lazily create media agents; they may be unconfigured in some deploys.
    thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
    video_agent = _safe_init(VideoAgent, "video")

    platforms = config.configured_platforms() or config.platforms
    pairings = _round_robin_platforms(config.content_pillars, platforms)

    created = 0
    for pillar, platform in pairings[: config.posts_per_run] or pairings:
        post = Post(pillar=pillar, platform=platform)
        try:
            db.insert(post)
            content_agent.generate(post)
            _generate_media(post, thumbnail_agent, video_agent)
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
    thumbnail_agent = _safe_init(ThumbnailAgent, "thumbnail")
    video_agent = _safe_init(VideoAgent, "video")

    scheduled = 0
    for post in posts:
        try:
            _generate_media(post, thumbnail_agent, video_agent)
            scheduler_agent.schedule(post)
            if persist_insert:
                db.insert(post)
            else:
                db.upsert(post)
            scheduled += 1
        except Exception:
            logger.exception("Failed finalising post %s", post.id)
            try:
                db.update_status(post, PostStatus.FAILED, error="finalise error")
            except Exception:
                logger.exception("Could not persist failure for post %s", post.id)
    return scheduled


def _generate_media(post: Post, thumbnail_agent, video_agent) -> None:
    """Best-effort media generation; never fatal to the pipeline."""
    needs_video = post.platform in _VIDEO_PLATFORMS

    if thumbnail_agent is not None:
        try:
            thumbnail_agent.generate(post)
        except Exception:
            logger.exception("Thumbnail generation failed for post %s", post.id)

    if needs_video and video_agent is not None:
        try:
            video_agent.generate(post)
        except Exception:
            logger.exception("Video generation failed for post %s", post.id)


def run_publisher() -> None:
    """Publish every post whose scheduled time has arrived."""
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
        # Atomically claim the post first; if another worker already took it,
        # skip — this is what prevents the same post going out twice.
        try:
            if not db.claim_for_publishing(post):
                continue
        except Exception:
            logger.exception("Could not claim post %s; skipping", post.id)
            continue

        try:
            publisher.publish(post)
        except Exception:
            # publish() already marked it failed; just keep going.
            logger.warning("Skipping failed post %s", post.id)
        finally:
            try:
                db.upsert(post)
            except Exception:
                logger.exception("Could not persist post %s after publish", post.id)


def _safe_init(agent_cls, label: str):
    """Instantiate an agent, returning None if it's unconfigured."""
    try:
        return agent_cls()
    except Exception as exc:
        logger.warning("%s agent unavailable: %s", label, exc)
        return None


def build_scheduler() -> BlockingScheduler:
    """Wire up the recurring jobs."""
    scheduler = BlockingScheduler(timezone=config.timezone)

    # Research trending topics and schedule content from them at 05:30 local,
    # ahead of the static content pipeline.
    scheduler.add_job(
        run_research_pipeline,
        trigger=CronTrigger(hour=5, minute=30, timezone=config.timezone),
        id="research_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Pick up human-approved topics and schedule them every 15 minutes, so
    # an approval goes live without waiting for the next day's run.
    scheduler.add_job(
        run_approved_pipeline,
        trigger=IntervalTrigger(minutes=15),
        id="approved_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # Generate and schedule content once a day at 06:00 local.
    scheduler.add_job(
        run_content_pipeline,
        trigger=CronTrigger(hour=6, minute=0, timezone=config.timezone),
        id="content_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # Check for due posts every 5 minutes.
    scheduler.add_job(
        run_publisher,
        trigger=IntervalTrigger(minutes=5),
        id="publisher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    return scheduler


def main() -> None:
    configure_logging(config.log_level)
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
