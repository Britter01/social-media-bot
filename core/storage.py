"""Supabase Storage uploader.

Pushes generated media bytes to a Supabase Storage bucket and returns a
public URL. This is the production target for the thumbnail agent's
``_persist`` seam: platforms like Instagram and TikTok need media at a
publicly reachable URL, not a local path.

Create the bucket once (public) in the Supabase dashboard, or via SQL:

    insert into storage.buckets (id, name, public)
    values ('media', 'media', true)
    on conflict (id) do nothing;
"""

from __future__ import annotations

import logging
import mimetypes

from core.config import Config, config

logger = logging.getLogger(__name__)


class Storage:
    """Uploads bytes to a Supabase Storage bucket and returns public URLs."""

    def __init__(self, cfg: Config = config) -> None:
        cfg.require("supabase_url", "supabase_key")
        from supabase import create_client

        self._cfg = cfg
        self._bucket = cfg.supabase_bucket
        self._client = create_client(cfg.supabase_url, cfg.supabase_key)
        logger.info("Storage ready (bucket=%s) at %s", self._bucket, cfg.supabase_url)

    def download(self, path: str) -> bytes | None:
        """Download bytes from ``path`` in the bucket.

        Returns None if the object does not exist (404).
        Re-raises on any other error so callers can distinguish a genuine
        cache miss from a transient connectivity problem.
        """
        try:
            return self._client.storage.from_(self._bucket).download(path)
        except Exception as exc:
            msg = str(exc).lower()
            if "not found" in msg or "404" in msg or "object not found" in msg:
                return None
            raise

    def delete(self, path: str) -> bool:
        """Delete ``path`` from the bucket.

        Returns True if the object was deleted (or didn't exist), False on error.
        """
        try:
            self._client.storage.from_(self._bucket).remove([path])
            logger.info("Deleted %s from bucket %s", path, self._bucket)
            return True
        except Exception:
            logger.exception("Failed to delete %s from bucket %s", path, self._bucket)
            return False

    def upload(
        self,
        path: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        """Upload ``data`` to ``path`` in the bucket; return its public URL.

        Uses upsert so re-runs for the same post id overwrite cleanly.
        """
        content_type = content_type or (mimetypes.guess_type(path)[0] or "application/octet-stream")
        bucket = self._client.storage.from_(self._bucket)
        try:
            # supabase-py forwards file options to the Storage API; values
            # must be strings. "upsert": "true" overwrites an existing object.
            bucket.upload(
                path=path,
                file=data,
                file_options={"content-type": content_type, "upsert": "true"},
            )
        except Exception:
            logger.exception("Failed to upload %s to bucket %s", path, self._bucket)
            raise

        public_url = bucket.get_public_url(path)
        logger.info("Uploaded %s -> %s", path, public_url)
        return public_url


_storage: Storage | None = None


def get_storage(cfg: Config = config) -> Storage:
    """Return a process-wide singleton ``Storage`` (lazy-initialised)."""
    global _storage
    if _storage is None:
        _storage = Storage(cfg)
    return _storage


def flag_is_set(path: str, *, on_error: bool, attempts: int = 3) -> bool:
    """Return True if the flag object at ``path`` exists.

    Config flags (pause switches, mode switches) are stored as the mere
    presence of an object. A *missing* object is an unambiguous "not set" and
    returns False immediately.

    Anything else — a dropped HTTP/2 connection, a 5xx, Storage being
    unreachable — is retried up to *attempts* times before falling back to
    ``on_error``. Callers must choose that fallback deliberately:

    * ``on_error=True`` for a flag that *blocks* an irreversible action (a
      publishing pause). If we cannot prove the pause is lifted, we must not
      publish: a delayed post is recoverable, a post the user never wanted
      is not.
    * ``on_error=False`` for a flag whose absence is the safe state.

    The retry matters because the process talks to Supabase over a pooled
    HTTP/2 connection that is periodically terminated server-side; a single
    read is not a reliable signal.
    """
    try:
        storage = get_storage()
    except Exception:
        # Storage isn't configured at all (dev, tests, a local run without
        # Supabase creds). No flag can exist, so this is "not set" rather than
        # an outage — failing closed here would wedge every environment that
        # simply doesn't use Storage.
        return False

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return storage.download(path) is not None
        except Exception as exc:  # noqa: BLE001 — any transport error must retry
            last_exc = exc
            if attempt < attempts - 1:
                import time

                time.sleep(0.5 * (2**attempt))
    logger.error(
        "Could not read flag %s after %d attempts — assuming %s. Last error: %s: %s",
        path,
        attempts,
        "SET" if on_error else "NOT SET",
        type(last_exc).__name__,
        last_exc,
    )
    return on_error
