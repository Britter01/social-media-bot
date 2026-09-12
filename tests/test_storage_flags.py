"""Tests for core.storage flag reading.

These cover the pause-flag semantics that decide whether a post is allowed to
go out. The bug they guard against: a paused platform published anyway because
a single dropped Supabase connection made the pause check return "not paused".
"""

from __future__ import annotations

import core.storage as storage_mod


class _FakeStorage:
    """Minimal stand-in for Storage with a scripted download() sequence."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def download(self, path):
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _use(monkeypatch, fake):
    monkeypatch.setattr(storage_mod, "get_storage", lambda *a, **k: fake)
    # Keep the retry backoff from actually sleeping in tests.
    monkeypatch.setattr("time.sleep", lambda _s: None)


def test_present_flag_is_set(monkeypatch):
    fake = _FakeStorage([b"paused"])
    _use(monkeypatch, fake)
    assert storage_mod.flag_is_set("config/platform_paused.instagram", on_error=True) is True
    assert fake.calls == 1


def test_missing_flag_is_not_set_without_retrying(monkeypatch):
    # A 404 is an unambiguous answer, not an outage — answer immediately.
    fake = _FakeStorage([None])
    _use(monkeypatch, fake)
    assert storage_mod.flag_is_set("config/platform_paused.instagram", on_error=True) is False
    assert fake.calls == 1


def test_transient_error_then_success_uses_the_real_answer(monkeypatch):
    # The dropped-HTTP/2-connection case seen in production: retry, don't guess.
    fake = _FakeStorage([ConnectionError("ConnectionTerminated"), b"paused"])
    _use(monkeypatch, fake)
    assert storage_mod.flag_is_set("config/platform_paused.instagram", on_error=True) is True
    assert fake.calls == 2


def test_persistent_error_fails_closed_for_a_pause_flag(monkeypatch):
    # This is the regression: every attempt fails, so we must NOT conclude
    # "not paused" and publish. A delayed post is recoverable; a sent one isn't.
    fake = _FakeStorage([ConnectionError("boom")] * 3)
    _use(monkeypatch, fake)
    assert storage_mod.flag_is_set("config/platform_paused.instagram", on_error=True) is True
    assert fake.calls == 3


def test_persistent_error_can_fail_open_when_absence_is_safe(monkeypatch):
    fake = _FakeStorage([ConnectionError("boom")] * 3)
    _use(monkeypatch, fake)
    assert storage_mod.flag_is_set("config/some_optional_flag", on_error=False) is False


def test_unconfigured_storage_is_not_set(monkeypatch):
    # Dev/test environments have no Supabase creds at all. No flag can exist,
    # so this must read as "not set" rather than wedging everything as paused.
    def _boom(*a, **k):
        raise RuntimeError("Missing required configuration: SUPABASE_KEY, SUPABASE_URL")

    monkeypatch.setattr(storage_mod, "get_storage", _boom)
    assert storage_mod.flag_is_set("config/platform_paused.instagram", on_error=True) is False
