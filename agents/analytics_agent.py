"""Analytics agent — fetches post-publish engagement metrics from each platform.

Two snapshot windows are collected per published post:
  * ``24h`` — fetched ~24 hours after publish.
  * ``7d``  — fetched ~7 days (168 hours) after publish.

The scheduler calls ``run_snapshot`` every 2 hours; each call finds posts in
the relevant window, pulls metrics, and upserts into ``post_analytics``.

Platform coverage:
  * Instagram  — Graph API insights endpoint.
  * Facebook   — Graph API post insights.
  * LinkedIn   — Organization share statistics.
  * TikTok     — Video query API.
  * YouTube    — YouTube Analytics API.
  * Twitter/X  — Skipped (paid tier required).

All platform fetchers catch every exception and return None gracefully so a
single broken token or rate-limit never aborts the whole run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import requests

from core.config import Config, config
from core.meta_token import get_user_token

logger = logging.getLogger(__name__)

_ANALYTICS_TABLE = "post_analytics"
# Keep in step with the publisher (v22.0). Several post_impressions metrics
# were deprecated on older versions, and a stale version is a common reason
# Facebook insights silently return nothing while likes/comments still work.
_GRAPH_BASE = "https://graph.facebook.com/v22.0"
_REQUEST_TIMEOUT = 10


class AnalyticsAgent:
    """Fetches and stores engagement metrics for published posts."""

    def __init__(self, cfg: Config = config) -> None:
        self._cfg = cfg
        from supabase import create_client

        self._client = create_client(cfg.supabase_url, cfg.supabase_key)

        # Run diagnostics, accumulated across this agent's run so the dashboard
        # can show *why* a fetch returned nothing instead of a blank tab.
        self._checked = 0
        self._stored = 0
        self._errors: list[str] = []
        self._last_error: str | None = None
        # Partial failures: a fetch that stored *something* (e.g. likes) but
        # could not get other metrics (e.g. Facebook views). These don't block
        # the row, but we surface them so a "likes but no views" case isn't
        # silently invisible.
        self._warnings: list[str] = []
        # LinkedIn personal-profile detection — only emit the "no analytics API"
        # error once per run, not once per post (avoids x24 noise in the summary).
        self._linkedin_no_org_logged = False
        # Per-platform checked/stored tallies so the summary can show exactly
        # which platforms are (and aren't) producing analytics — otherwise a
        # platform that is never even queried (e.g. Facebook posts outside the
        # snapshot window) is invisible behind another platform's error noise.
        self._plat_checked: dict[str, int] = {}
        self._plat_stored: dict[str, int] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def summary(self) -> str:
        """Human-readable summary of the most recent run for the dashboard."""
        from collections import Counter

        parts = [f"checked {self._checked} post(s), stored {self._stored}"]
        # Per-platform breakdown (checked/stored) — makes "Facebook isn't being
        # picked up at all" vs "Facebook is fetched but stores nothing" obvious.
        if self._plat_checked:
            breakdown = ", ".join(
                f"{plat} {self._plat_stored.get(plat, 0)}/{n}"
                for plat, n in sorted(self._plat_checked.items())
            )
            parts.append(f"by platform: {breakdown}")
        if self._stored == 0 and self._errors:
            top = Counter(self._errors).most_common(2)
            parts.append("; ".join(f"{msg} (x{n})" for msg, n in top))
        elif self._stored == 0:
            parts.append("no metrics returned (check platform tokens/permissions)")
        # Always surface partial-failure warnings (e.g. likes stored but views
        # could not be fetched) so a "likes but no views" case is explained.
        if self._warnings:
            top = Counter(self._warnings).most_common(2)
            parts.append("partial: " + "; ".join(f"{msg} (x{n})" for msg, n in top))
        return " — ".join(parts)

    def _record(self, post_id: str, platform: str, metrics: dict | None) -> bool:
        """Track diagnostics for one fetch; return True if metrics were usable."""
        self._checked += 1
        self._plat_checked[platform] = self._plat_checked.get(platform, 0) + 1
        if metrics is None:
            if self._last_error:
                self._errors.append(self._last_error)
            return False
        return True

    def fetch_metrics(
        self,
        post_id: str,
        platform: str,
        platform_post_id: str,
        snapshot_type: str,
    ) -> dict | None:
        """Fetch engagement metrics for one post from the appropriate platform API.

        Returns a flat dict of metric names → values (integers or None) ready
        to upsert into ``post_analytics``, or None if nothing could be fetched.
        """
        fetcher = {
            "instagram": self._fetch_instagram,
            "facebook": self._fetch_facebook,
            "linkedin": self._fetch_linkedin,
            "tiktok": self._fetch_tiktok,
            "youtube": self._fetch_youtube,
            "twitter": self._fetch_twitter,
        }.get(platform.lower())

        if fetcher is None:
            logger.debug("No analytics fetcher for platform %r; skipping", platform)
            return None

        return fetcher(platform_post_id)

    def run_backfill(self) -> int:
        """Fetch metrics for all published posts that have no analytics row yet.

        This catches posts that were published before the analytics table existed,
        or that fell outside the narrow scheduled-snapshot windows. Stores a '7d'
        snapshot (since the post is older than the immediate windows). Returns the
        number of rows successfully upserted.
        """
        from core.database import get_database

        db = get_database(self._cfg)
        posts = db.get_all_published_without_analytics()
        if not posts:
            logger.debug("Backfill: no published posts without analytics")
            return 0

        logger.info("Backfill: %d post(s) have no analytics — fetching now", len(posts))
        count = 0
        for row in posts:
            post_id = row["id"]
            platform = row.get("platform", "")
            platform_post_id = row.get("platform_post_id", "")
            try:
                self._last_error = None
                metrics = self.fetch_metrics(post_id, platform, platform_post_id, "7d")
                if not self._record(post_id, platform, metrics):
                    # Permanently invalid post IDs (e.g. "does not exist" from the
                    # Graph API) would be retried on every backfill run forever.
                    # Tombstone them with an all-null row so they drop out of the
                    # no-analytics queue without polluting real metric totals.
                    err = self._last_error or ""
                    _permanent = (
                        "does not exist" in err
                        or "missing permissions" in err
                        or "does not support this operation" in err
                    )
                    if _permanent:
                        logger.info(
                            "Backfill: tombstoning permanently-invalid post %s (%s): %s",
                            post_id[:8],
                            platform,
                            err[:120],
                        )
                        self._upsert(
                            post_id,
                            platform,
                            platform_post_id,
                            "7d",
                            {"raw_data": {"tombstone": True, "error": err[:400]}},
                        )
                    else:
                        logger.debug("Backfill: no metrics for post %s (%s)", post_id[:8], platform)
                    continue
                self._upsert(post_id, platform, platform_post_id, "7d", metrics)
                count += 1
                self._stored += 1
                self._plat_stored[platform] = self._plat_stored.get(platform, 0) + 1
            except Exception as exc:
                self._errors.append(f"{platform}: {exc}")
                logger.exception("Backfill failed for post %s (%s)", post_id[:8], platform)
        logger.info("Backfill: stored %d snapshot(s)", count)
        return count

    def run_snapshot(self, snapshot_type: str) -> int:
        """Fetch and store metrics for all posts due for this snapshot.

        ``snapshot_type`` must be ``'24h'`` or ``'7d'``.  Returns the number
        of rows successfully upserted into ``post_analytics``.
        """
        hours = {"24h": 24, "7d": 168}.get(snapshot_type)
        if hours is None:
            logger.error("Unknown snapshot_type %r", snapshot_type)
            return 0

        from core.database import get_database

        db = get_database(self._cfg)
        posts = db.get_posts_needing_analytics(snapshot_type, hours)
        if not posts:
            logger.debug("No posts need %s analytics snapshot", snapshot_type)
            return 0

        logger.info("%d post(s) need %s analytics snapshot", len(posts), snapshot_type)
        count = 0
        for row in posts:
            post_id = row["id"]
            platform = row.get("platform", "")
            platform_post_id = row.get("platform_post_id", "")
            try:
                self._last_error = None
                metrics = self.fetch_metrics(post_id, platform, platform_post_id, snapshot_type)
                if not self._record(post_id, platform, metrics):
                    logger.debug("No metrics returned for post %s (%s)", post_id[:8], platform)
                    continue
                self._upsert(post_id, platform, platform_post_id, snapshot_type, metrics)
                count += 1
                self._stored += 1
                self._plat_stored[platform] = self._plat_stored.get(platform, 0) + 1
            except Exception:
                logger.exception(
                    "Failed to fetch/store analytics for post %s (%s)", post_id[:8], platform
                )
        logger.info("Stored %d %s snapshot(s)", count, snapshot_type)
        return count

    def run_views_refresh(self, platforms: list[str] | None = None) -> int:
        """Re-fetch views for posts that have engagement but no view metrics.

        Targets posts that already have an analytics row (so they're excluded
        from the normal backfill) but where impressions, reach, and video_views
        are all NULL — typically because the metric names changed after the rows
        were first written (e.g. the 2026-06-15 Facebook post_impressions ->
        post_views deprecation).

        Only updates the view columns; does NOT overwrite existing likes/comments.
        Returns the number of rows refreshed.
        """
        from core.database import get_database

        _platforms = platforms or ["facebook", "instagram"]
        db = get_database(self._cfg)
        posts = db.get_posts_missing_views(_platforms)
        if not posts:
            logger.debug("Views refresh: no posts with missing view metrics")
            return 0

        logger.info(
            "Views refresh: %d post(s) have engagement but no view metrics — re-fetching",
            len(posts),
        )
        count = 0
        for row in posts:
            post_id = row["id"]
            platform = row.get("platform", "")
            platform_post_id = row.get("platform_post_id", "")
            try:
                self._last_error = None
                metrics = self.fetch_metrics(post_id, platform, platform_post_id, "7d")
                if not metrics:
                    logger.debug(
                        "Views refresh: still no metrics for %s (%s)", post_id[:8], platform
                    )
                    continue
                # Only update view columns — don't touch existing engagement counts.
                view_only = {
                    k: metrics[k]
                    for k in ("impressions", "reach", "video_views")
                    if metrics.get(k) is not None
                }
                if not view_only:
                    logger.debug(
                        "Views refresh: views still null for %s (%s) — metric name may differ",
                        post_id[:8],
                        platform,
                    )
                    if self._last_error or self._warnings:
                        err = self._last_error or (self._warnings[-1] if self._warnings else "")
                        self._errors.append(f"views refresh {platform}: {err[:200]}")
                    continue
                # Patch the existing analytics row(s) with the new view values.
                # A post can hold both a 24h and a 7d snapshot row — only touch
                # rows whose view columns are still NULL, so a snapshot that
                # already captured views is never overwritten with values from
                # a different time window.
                try:
                    self._client.table(_ANALYTICS_TABLE).update(view_only).eq(
                        "post_id", post_id
                    ).is_("impressions", "null").is_("reach", "null").is_(
                        "video_views", "null"
                    ).execute()
                    self._plat_stored[platform] = self._plat_stored.get(platform, 0) + 1
                    self._stored += 1
                    count += 1
                    logger.info(
                        "Views refresh: patched %s (%s) — %s",
                        post_id[:8],
                        platform,
                        view_only,
                    )
                except Exception:
                    logger.exception(
                        "Views refresh: failed to patch post %s (%s)", post_id[:8], platform
                    )
            except Exception as exc:
                self._errors.append(f"views refresh {platform}: {exc}")
                logger.exception(
                    "Views refresh: fetch failed for post %s (%s)", post_id[:8], platform
                )
        logger.info("Views refresh: patched %d post(s)", count)
        return count

    # ── Platform fetchers ─────────────────────────────────────────────────────

    def _fetch_instagram(self, platform_post_id: str) -> dict | None:
        """Fetch Instagram engagement metrics via the Graph API.

        Two sources are combined for robustness:
          1. The media node itself (``like_count``, ``comments_count``) — these
             work with basic permissions and never depend on the insights scope.
          2. The insights endpoint for ``reach`` (and ``views``/``saved``/
             ``shares`` where available) — needs ``instagram_manage_insights``.

        Insights metrics changed in 2025: ``impressions`` was deprecated for
        media (replaced by ``views``) and ``comments_count`` is a node field,
        not an insight. The Graph API rejects the *whole* insights request if
        any single metric is invalid, so we try progressively smaller metric
        sets and keep whatever the first successful call returns. Even if every
        insights call fails, the like/comment counts from step 1 are still
        returned — so something always shows up.
        """
        token = get_user_token(self._cfg)
        if not token:
            logger.debug("Instagram access token not set; skipping")
            return None

        metrics: dict[str, Any] = {}
        raw: dict[str, Any] = {}

        # 1. Basic counts from the media node (reliable, minimal permissions).
        try:
            node = requests.get(
                f"{_GRAPH_BASE}/{platform_post_id}",
                params={
                    "fields": "like_count,comments_count,media_type",
                    "access_token": token,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            node.raise_for_status()
            ndata = node.json()
            raw["node"] = ndata
            metrics["likes"] = _int(ndata.get("like_count"))
            metrics["comments"] = _int(ndata.get("comments_count"))
        except Exception as exc:
            self._last_error = f"Instagram media read: {_graph_error(exc)}"
            logger.warning(
                "Instagram media-node fetch failed for %s: %s", platform_post_id[:12], exc
            )

        # 2. Insights — try richest valid set first, fall back on rejection.
        insights_err: str | None = None
        for metric_set in ("reach,views,saved,shares", "reach,saved", "reach"):
            try:
                resp = requests.get(
                    f"{_GRAPH_BASE}/{platform_post_id}/insights",
                    params={"metric": metric_set, "access_token": token},
                    timeout=_REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
            except Exception as exc:
                insights_err = f"Instagram insights: {_graph_error(exc)}"
                logger.warning(
                    "Instagram insights [%s] failed for %s: %s",
                    metric_set,
                    platform_post_id[:12],
                    exc,
                )
                continue
            data = resp.json()
            raw["insights"] = data
            insights_err = None
            for item in data.get("data", []):
                name = item.get("name", "")
                values = item.get("values") or []
                val = values[0].get("value") if values else item.get("value")
                if name == "reach":
                    metrics["reach"] = _int(val)
                elif name == "views":
                    metrics["impressions"] = _int(val)
                elif name == "saved":
                    metrics["saves"] = _int(val)
                elif name == "shares":
                    metrics["shares"] = _int(val)
            break  # first successful call wins

        metrics["raw_data"] = raw
        # Only treat as "no data" if literally nothing came back.
        meaningful = ("reach", "impressions", "likes", "comments", "saves", "shares")
        if not any(metrics.get(k) is not None for k in meaningful):
            # Surface the most useful reason: a node-read failure (token/id
            # problem) takes priority over an insights-only failure.
            if not self._last_error:
                self._last_error = insights_err or (
                    "Instagram returned no metrics (token may lack "
                    "instagram_manage_insights, or the id isn't a media id)"
                )
            logger.warning("Instagram: no metrics retrievable for %s", platform_post_id[:12])
            return None
        # Got at least likes/comments — clear any insights-only error.
        self._last_error = None
        return metrics

    def _facebook_page_token(self, user_token: str) -> str | None:
        """Derive a Page access token from a user access token.

        The publisher uses the same derivation (GET /{page_id}?fields=access_token).
        Page post insights require a page token — a user token only works for
        the post node (likes/comments) but NOT for the /insights endpoint, which
        is why views were missing even though likes/comments were stored.
        """
        page_id = self._cfg.facebook_page_id
        if not page_id:
            return None
        try:
            resp = requests.get(
                f"{_GRAPH_BASE}/{page_id}",
                params={"fields": "access_token", "access_token": user_token},
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.ok:
                return resp.json().get("access_token") or None
        except Exception as exc:
            logger.debug("Could not derive Facebook page token: %s", exc)
        return None

    def _fetch_facebook(self, platform_post_id: str) -> dict | None:
        """Fetch Facebook post engagement via the Graph API.

        Three sources are combined for robustness (mirroring the Instagram path):
          1. The post node — ``reactions.summary`` and ``comments.summary``.
             These plain fields exist on BOTH feed posts and photo objects, so
             they work whether the post was published via /feed or /photos.
          2. The ``shares`` field — requested separately and best-effort,
             because photo objects (posts published via /photos) have no
             ``shares`` field and the Graph API rejects the *whole* request if
             any requested field is invalid.
          3. The insights endpoint for ``post_impressions`` and
             ``post_video_views`` — best-effort, with progressive fallback.
             Insights metric names change often and the Graph API rejects the
             *whole* request if any single metric is invalid, so a failure here
             never discards the node counts.

        Important: post insights require a **Page access token**, not a user
        access token. This function derives one from the user token if an
        explicit FACEBOOK_PAGE_ACCESS_TOKEN is not configured — mirroring how
        the publisher obtains its token.
        """
        user_token = get_user_token(self._cfg)
        token = self._cfg.facebook_page_access_token
        _using_user_token = False
        if not token:
            if user_token:
                derived = self._facebook_page_token(user_token)
                if derived:
                    token = derived
                else:
                    # Page token derivation failed — fall back to user token but
                    # flag it so we can surface a clear warning when insights fail.
                    # A user token can read post nodes (likes/comments) but the
                    # /insights endpoint rejects it with (#100), which looks like
                    # an invalid-metric error rather than a permission error.
                    token = user_token
                    _using_user_token = True
                    logger.warning(
                        "Facebook: could not derive a page token from the user token "
                        "(check that the token has pages_show_list permission). "
                        "Falling back to user token — post node data will still be "
                        "fetched but insights (impressions/reach/views) will likely fail."
                    )
            else:
                token = None
        if not token:
            self._last_error = "Facebook: no access token (set FACEBOOK_PAGE_ACCESS_TOKEN)"
            logger.debug("Facebook/Instagram access token not set; skipping")
            return None

        metrics: dict[str, Any] = {}
        raw: dict[str, Any] = {}

        # 1. Engagement + comments from the node. Feed posts expose
        # ``reactions``; photo objects (posts published via /photos) expose
        # ``likes`` instead and reject ``reactions`` outright. The Graph API
        # fails the whole request on one invalid field, so try the reactions
        # field-set first and fall back to the likes field-set.
        node_err: str | None = None
        for fieldset, eng_field in (
            (
                "reactions.summary(total_count).limit(0),comments.summary(total_count).limit(0)",
                "reactions",
            ),
            ("likes.summary(total_count).limit(0),comments.summary(total_count).limit(0)", "likes"),
        ):
            try:
                node = requests.get(
                    f"{_GRAPH_BASE}/{platform_post_id}",
                    params={"fields": fieldset, "access_token": token},
                    timeout=_REQUEST_TIMEOUT,
                )
                node.raise_for_status()
            except Exception as exc:
                node_err = f"Facebook node read: {_graph_error(exc)}"
                logger.warning(
                    "Facebook node fetch [%s] failed for %s: %s",
                    eng_field,
                    platform_post_id[:12],
                    exc,
                )
                continue
            ndata = node.json()
            raw["node"] = ndata
            engagement = (ndata.get(eng_field) or {}).get("summary") or {}
            comments = (ndata.get("comments") or {}).get("summary") or {}
            metrics["likes"] = _int(engagement.get("total_count"))
            metrics["comments"] = _int(comments.get("total_count"))
            node_err = None
            break
        if node_err:
            self._last_error = node_err

        # 2. Shares — separate best-effort call (absent on photo objects).
        try:
            sresp = requests.get(
                f"{_GRAPH_BASE}/{platform_post_id}",
                params={"fields": "shares", "access_token": token},
                timeout=_REQUEST_TIMEOUT,
            )
            sresp.raise_for_status()
            shares = sresp.json().get("shares") or {}
            metrics["shares"] = _int(shares.get("count"))
        except Exception as exc:
            logger.debug("Facebook shares unavailable for %s: %s", platform_post_id[:12], exc)

        # 3. Insights — views/reach, best-effort with fallback.
        #
        # 2026-06-15 deprecation: Meta removed post_impressions*, post_video_views,
        # AND post_views* for ALL Graph API versions. The confirmed replacement is
        # post_media_view (announced by Meta partner tooling such as Yext/Sprout
        # Social). Try the new name first, then the intermediate post_views names
        # we tried, then the original legacy names for any pre-deprecation data.
        # The Graph API rejects the whole request if ANY metric is invalid, so
        # each set is tried independently.
        insights_err: str | None = None
        _insight_params: list[dict] = [
            {"metric": m, "period": "lifetime"}
            for m in (
                # Current replacement (confirmed 2026-06-15 deprecation).
                "post_media_view_unique,post_media_view",
                "post_media_view",
                # Intermediate names tried before confirmed replacement.
                "post_views_unique,post_views",
                "post_views",
                # Legacy names (valid on data created before 2026-06-15).
                "post_impressions_unique,post_impressions,post_video_views",
                "post_impressions",
            )
        ] + [{"metric": "post_media_view"}]  # no period — let the API infer it
        for params in _insight_params:
            try:
                resp = requests.get(
                    f"{_GRAPH_BASE}/{platform_post_id}/insights",
                    params={**params, "access_token": token},
                    timeout=_REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
            except Exception as exc:
                insights_err = f"Facebook insights: {_graph_error(exc)}"
                logger.warning(
                    "Facebook insights [%s] failed for %s: %s",
                    params.get("metric", "?")[:40],
                    platform_post_id[:12],
                    exc,
                )
                continue
            data = resp.json()
            raw["insights"] = data
            insights_err = None
            for item in data.get("data", []):
                name = item.get("name", "")
                values = item.get("values") or []
                val = values[0].get("value") if values else item.get("value")
                # Map all known names (current + intermediate + legacy) onto
                # the same stored columns so the dashboard is naming-agnostic.
                if name in ("post_media_view", "post_views", "post_impressions"):
                    metrics["impressions"] = _int(val)
                elif name in (
                    "post_media_view_unique",
                    "post_views_unique",
                    "post_impressions_unique",
                ):
                    metrics["reach"] = _int(val)
                elif name == "post_video_views":
                    metrics["video_views"] = _int(val)
            break  # first successful call wins

        metrics["raw_data"] = raw
        meaningful = ("impressions", "reach", "likes", "comments", "shares", "video_views")
        if not any(metrics.get(k) is not None for k in meaningful):
            if not self._last_error:
                self._last_error = insights_err or (
                    "Facebook returned no metrics (check the token has "
                    "pages_read_engagement and the id is a real page-post id)"
                )
            return None
        # Got at least node-level engagement. If views/impressions specifically
        # could not be fetched, record a *warning* (the row still stores) so the
        # "likes but no views" case is visible rather than silently swallowed.
        got_views = any(metrics.get(k) is not None for k in ("impressions", "reach", "video_views"))
        if not got_views and insights_err:
            if _using_user_token:
                self._warnings.append(
                    "Facebook views missing: page token could not be derived "
                    "(add pages_show_list + pages_read_engagement to your Meta app, "
                    "or set FACEBOOK_PAGE_ACCESS_TOKEN directly)"
                )
            elif "(#100)" in insights_err:
                self._warnings.append(
                    "Facebook views/reach missing: the page token lacks the "
                    "pages_read_engagement permission. Fix: (1) add pages_read_engagement "
                    "to your Meta App permissions, (2) re-authorise to get a new user token "
                    "with that scope, (3) use Refresh Meta Token to exchange it. "
                    "Engagement (likes/comments/shares) is still captured."
                )
            else:
                self._warnings.append(insights_err)
        self._last_error = None
        return metrics

    def _fetch_linkedin(self, platform_post_id: str) -> dict | None:
        """Fetch LinkedIn share statistics — organization pages only.

        LinkedIn only exposes post analytics for **organization (company
        page)** shares, via ``organizationalEntityShareStatistics`` with the
        ``r_organization_social`` scope. **Personal-profile** posts have no
        analytics API: the member-level scope (``r_member_social``) that once
        allowed reading a member's own social actions is no longer granted to
        new applications. So for a personal profile there is simply no path to
        reach/impression/like data — we detect that and skip with a clear,
        non-actionable message rather than spamming 403s.
        """
        token = self._cfg.linkedin_access_token
        if not token:
            self._last_error = "LinkedIn: no access token (set LINKEDIN_ACCESS_TOKEN)"
            logger.debug("LinkedIn access token not set; skipping")
            return None

        # Only organization URNs have an analytics endpoint. A configured
        # LINKEDIN_AUTHOR_URN of the form urn:li:organization:NNN means a
        # company page; anything else (or a token that only resolves to a
        # person) is a personal profile with no analytics API.
        org_urn = self._cfg.linkedin_author_urn or ""
        if "urn:li:organization" not in org_urn:
            if not self._linkedin_no_org_logged:
                # Emit this configuration fact only once per agent run so it
                # appears as (x1) in the summary rather than (x24).
                self._last_error = (
                    "LinkedIn: personal-profile post analytics are not available via the API "
                    "(only company pages expose stats; r_member_social is no longer granted)"
                )
                self._linkedin_no_org_logged = True
                logger.info(
                    "LinkedIn personal profile — analytics API unavailable; "
                    "skipping all LinkedIn posts"
                )
            else:
                self._last_error = None  # already counted; don't re-add to _errors
            return None

        try:
            resp = requests.get(
                "https://api.linkedin.com/v2/organizationalEntityShareStatistics",
                params={
                    "q": "organizationalEntity",
                    "organizationalEntity": org_urn,
                    "shares[0]": platform_post_id,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Restli-Protocol-Version": "2.0.0",
                },
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code in (401, 403):
                self._last_error = (
                    f"LinkedIn: {resp.status_code} "
                    "(token may lack r_organization_social scope for this company page)"
                )
                logger.debug(
                    "LinkedIn analytics: %d (token may lack analytics scope)", resp.status_code
                )
                return None
            resp.raise_for_status()
            data = resp.json()
            elements = data.get("elements", [])
            metrics: dict[str, Any] = {"raw_data": data}
            for el in elements:
                stats = el.get("totalShareStatistics", {})
                metrics["impressions"] = _int(stats.get("impressionCount"))
                metrics["likes"] = _int(stats.get("likeCount"))
                metrics["comments"] = _int(stats.get("commentCount"))
                metrics["shares"] = _int(stats.get("shareCount"))
                metrics["reach"] = _int(stats.get("uniqueImpressionsCount"))
            return metrics
        except Exception as exc:
            self._last_error = f"LinkedIn: {exc}"
            logger.exception("LinkedIn analytics fetch failed for %s", platform_post_id[:12])
            return None

    def _fetch_tiktok(self, platform_post_id: str) -> dict | None:
        """Fetch TikTok video stats."""
        token = self._cfg.tiktok_access_token
        if not token:
            logger.debug("TikTok access token not set; skipping")
            return None
        try:
            resp = requests.post(
                "https://open.tiktokapis.com/v2/video/query/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "filters": {"video_ids": [platform_post_id]},
                    "fields": [
                        "id",
                        "view_count",
                        "like_count",
                        "comment_count",
                        "share_count",
                    ],
                },
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            videos = (data.get("data") or {}).get("videos", [])
            if not videos:
                return None
            v = videos[0]
            return {
                "video_views": _int(v.get("view_count")),
                "likes": _int(v.get("like_count")),
                "comments": _int(v.get("comment_count")),
                "shares": _int(v.get("share_count")),
                "raw_data": data,
            }
        except Exception:
            logger.exception("TikTok analytics fetch failed for %s", platform_post_id[:12])
            return None

    def _fetch_youtube(self, platform_post_id: str) -> dict | None:
        """Fetch YouTube video analytics via the YouTube Analytics API."""
        refresh_token = self._cfg.youtube_refresh_token
        client_id = self._cfg.youtube_client_id
        client_secret = self._cfg.youtube_client_secret
        if not all([refresh_token, client_id, client_secret]):
            logger.debug("YouTube credentials not set; skipping")
            return None
        try:
            # Exchange refresh token for an access token.
            token_resp = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            token_resp.raise_for_status()
            access_token = token_resp.json().get("access_token")
            if not access_token:
                logger.debug("YouTube token exchange returned no access_token")
                return None

            today = datetime.now(UTC).date().isoformat()
            analytics_resp = requests.get(
                "https://youtubeanalytics.googleapis.com/v2/reports",
                params={
                    "ids": "channel==MINE",
                    "startDate": "2020-01-01",
                    "endDate": today,
                    "metrics": "views,likes,comments,shares,estimatedMinutesWatched",
                    "filters": f"video=={platform_post_id}",
                    "dimensions": "video",
                },
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=_REQUEST_TIMEOUT,
            )
            analytics_resp.raise_for_status()
            data = analytics_resp.json()
            rows = data.get("rows", [])
            if not rows:
                return None
            # columns: video, views, likes, comments, shares, estimatedMinutesWatched
            row = rows[0]
            return {
                "video_views": _int(row[1]) if len(row) > 1 else None,
                "likes": _int(row[2]) if len(row) > 2 else None,
                "comments": _int(row[3]) if len(row) > 3 else None,
                "shares": _int(row[4]) if len(row) > 4 else None,
                "raw_data": data,
            }
        except Exception:
            logger.exception("YouTube analytics fetch failed for %s", platform_post_id[:12])
            return None

    def _fetch_twitter(self, platform_post_id: str) -> dict | None:
        """Twitter/X analytics require a paid API tier — skip silently."""
        return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _upsert(
        self,
        post_id: str,
        platform: str,
        platform_post_id: str,
        snapshot_type: str,
        metrics: dict,
    ) -> None:
        """Upsert a post_analytics row."""
        raw = metrics.pop("raw_data", None)
        row = {
            "post_id": post_id,
            "platform": platform,
            "platform_post_id": platform_post_id,
            "snapshot_type": snapshot_type,
            "fetched_at": datetime.now(UTC).isoformat(),
            "reach": metrics.get("reach"),
            "impressions": metrics.get("impressions"),
            "likes": metrics.get("likes"),
            "comments": metrics.get("comments"),
            "shares": metrics.get("shares"),
            "saves": metrics.get("saves"),
            "video_views": metrics.get("video_views"),
            # Pass the dict through as-is — json.dumps here would store a JSON
            # *string* scalar in the jsonb column, making it unqueryable.
            "raw_data": raw,
        }
        try:
            self._client.table(_ANALYTICS_TABLE).upsert(
                row, on_conflict="post_id,snapshot_type"
            ).execute()
            logger.debug(
                "Upserted %s analytics for post %s (reach=%s)",
                snapshot_type,
                post_id[:8],
                row.get("reach"),
            )
        except Exception:
            logger.exception("Failed to upsert analytics for post %s", post_id[:8])
            raise


# ── Performance digest ────────────────────────────────────────────────────────


def generate_performance_digest(days: int = 30) -> str:
    """Query post_analytics and return a human-readable performance summary.

    Returns an empty string if no analytics data exists yet so callers can
    safely skip injecting it into prompts.
    """
    try:
        from datetime import timedelta

        from supabase import create_client

        cfg = config
        client = create_client(cfg.supabase_url, cfg.supabase_key)

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        # Fetch analytics joined with posts (via separate queries to stay simple).
        analytics_resp = (
            client.table(_ANALYTICS_TABLE).select("*").gte("fetched_at", cutoff).execute()
        )
        analytics_rows = analytics_resp.data or []
        if not analytics_rows:
            return ""

        # Fetch matching posts.
        post_ids = list({row["post_id"] for row in analytics_rows})
        posts_resp = (
            client.table("posts")
            .select("id, title, topic, pillar, platform, published_time, post_type")
            .in_("id", post_ids)
            .execute()
        )
        posts_by_id = {p["id"]: p for p in (posts_resp.data or [])}

        # Prefer 7d snapshot over 24h for the same post.
        best: dict[str, dict] = {}
        for row in analytics_rows:
            pid = row["post_id"]
            existing = best.get(pid)
            if existing is None or row["snapshot_type"] == "7d":
                best[pid] = row

        rows = list(best.values())
        if not rows:
            return ""

        def _reach(r: dict) -> int:
            return r.get("reach") or r.get("impressions") or 0

        # Enrich rows with post metadata.
        enriched = []
        for r in rows:
            post = posts_by_id.get(r["post_id"], {})
            enriched.append({**r, "_post": post})

        # Sort by reach descending.
        enriched.sort(key=lambda x: _reach(x), reverse=True)

        top = enriched[:5]
        bottom = enriched[-5:] if len(enriched) >= 5 else []
        bottom.sort(key=lambda x: _reach(x))

        def _title(r: dict) -> str:
            p = r["_post"]
            return p.get("title") or p.get("topic") or "Untitled"

        def _fmt(n: int) -> str:
            if n >= 1000:
                return f"{n / 1000:.1f}k"
            return str(n)

        # Pillar averages.
        from collections import defaultdict

        pillar_reach: dict[str, list[int]] = defaultdict(list)
        platform_reach: dict[str, list[int]] = defaultdict(list)
        for r in enriched:
            reach = _reach(r)
            pillar = r["_post"].get("pillar") or "Unknown"
            platform = r.get("platform") or "Unknown"
            pillar_reach[pillar].append(reach)
            platform_reach[platform].append(reach)

        def _avg(lst: list[int]) -> int:
            return int(sum(lst) / len(lst)) if lst else 0

        pillar_avgs = sorted(((p, _avg(v)) for p, v in pillar_reach.items()), key=lambda x: -x[1])

        # Best posting times (hour of published_time).
        from collections import Counter

        hour_counts: Counter = Counter()
        for r in enriched:
            pub = r["_post"].get("published_time") or ""
            if pub:
                try:
                    dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    hour_counts[dt.strftime("%A %H:00")] += _reach(r)
                except Exception:
                    pass
        top_hours = [h for h, _ in hour_counts.most_common(2)]

        # Build the digest string.
        lines = [f"=== PERFORMANCE DIGEST (last {days} days) ==="]

        lines.append("TOP PERFORMERS by reach:")
        for r in top:
            post = r["_post"]
            t = _title(r)
            plat = r.get("platform", "")
            pillar = post.get("pillar", "")
            reach = _reach(r)
            likes = r.get("likes") or 0
            snap = r.get("snapshot_type", "")
            lines.append(
                f'- "{t}" ({plat}, {pillar}): {_fmt(reach)} reach, '
                f"{_fmt(likes)} likes [{snap} snapshot]"
            )

        lines.append("")
        if pillar_avgs:
            best_pillars = ", ".join(f"{p} (avg {_fmt(v)} reach)" for p, v in pillar_avgs[:2])
            worst_pillars = ", ".join(f"{p} (avg {_fmt(v)} reach)" for p, v in pillar_avgs[-2:])
            lines.append(f"BEST PILLARS: {best_pillars}")
            lines.append(f"WORST PILLARS: {worst_pillars}")

        if top_hours:
            lines.append(f"BEST POSTING TIMES: {', '.join(top_hours)}")

        if bottom:
            avoid_titles = ", ".join(f'"{_title(r)}"' for r in bottom[:3])
            bottom_reach = _fmt(_avg([_reach(r) for r in bottom]))
            lines.append(f"AVOID: Posts about {avoid_titles} averaged only {bottom_reach} reach.")

        lines.append("===")
        return "\n".join(lines)

    except Exception:
        logger.exception("Failed to generate performance digest")
        return ""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _int(val: Any) -> int | None:
    """Safely cast a value to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _graph_error(exc: Exception) -> str:
    """Extract a concise, human-readable message from a Graph API error.

    Facebook/Instagram return useful detail in the JSON body
    (``error.message`` + ``error.code``); pull that out so the dashboard
    shows "(#100) ... metric not supported" rather than a bare "400".
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            err = (resp.json() or {}).get("error") or {}
            msg = err.get("message")
            code = err.get("code")
            if msg:
                return f"(#{code}) {msg}" if code is not None else msg
        except Exception:
            text = getattr(resp, "text", "") or ""
            if text:
                return text[:200]
        status = getattr(resp, "status_code", "")
        if status:
            return f"HTTP {status}"
    return str(exc)[:200]
