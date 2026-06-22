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
