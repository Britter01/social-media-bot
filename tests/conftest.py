"""Test fixtures and lightweight fakes for the external SDKs.

The agents depend on heavy third-party SDKs (anthropic, google-genai,
supabase, apscheduler, ...). To keep the unit tests fast and hermetic —
no network, no real credentials, no need to ``pip install`` every SDK —
this conftest injects minimal fake modules into ``sys.modules`` for any
SDK that isn't already installed. If the real package *is* installed
(e.g. in CI after ``pip install -r requirements.txt``), the real one is
left in place; the tests never make network calls regardless, because
they replace the client objects on each agent with mocks.
"""

from __future__ import annotations

import dataclasses
import sys
import types
from unittest.mock import MagicMock

import pytest


def _fake_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _ensure(name: str) -> bool:
    """Return True if ``name`` already imports; False if we must fake it."""
    if name in sys.modules:
        return True
    try:
        __import__(name)
        return True
    except Exception:
        return False


# --- pydantic ---------------------------------------------------------
if not _ensure("pydantic"):
    pyd = _fake_module("pydantic")

    class _BaseModel:  # minimal stand-in
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    def _Field(default=None, **_kwargs):
        return default

    pyd.BaseModel = _BaseModel
    pyd.Field = _Field


# --- anthropic --------------------------------------------------------
if not _ensure("anthropic"):
    a = _fake_module("anthropic")

    class _Anthropic:
        def __init__(self, *args, **kwargs):
            self.messages = MagicMock()

    class _APIError(Exception):
        pass

    a.Anthropic = _Anthropic
    a.APIError = _APIError
    a.BadRequestError = type("BadRequestError", (_APIError,), {})


# --- google-genai -----------------------------------------------------
if not _ensure("google.genai"):
    google_pkg = sys.modules.get("google") or _fake_module("google")
    genai = _fake_module("google.genai")
    google_pkg.genai = genai

    class _GenAIClient:
        def __init__(self, *args, **kwargs):
            self.models = MagicMock()

    genai.Client = _GenAIClient

    gtypes = _fake_module("google.genai.types")
    genai.types = gtypes

    class _GenerateImagesConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    gtypes.GenerateImagesConfig = _GenerateImagesConfig


# --- supabase ---------------------------------------------------------
if not _ensure("supabase"):
    s = _fake_module("supabase")
    s.create_client = lambda *args, **kwargs: MagicMock()


# --- requests_oauthlib ------------------------------------------------
if not _ensure("requests_oauthlib"):
    r = _fake_module("requests_oauthlib")
    r.OAuth1Session = MagicMock


# --- apscheduler ------------------------------------------------------
if not _ensure("apscheduler"):
    _fake_module("apscheduler")
    _fake_module("apscheduler.schedulers")
    blocking = _fake_module("apscheduler.schedulers.blocking")
    blocking.BlockingScheduler = MagicMock
    _fake_module("apscheduler.triggers")
    cron = _fake_module("apscheduler.triggers.cron")
    cron.CronTrigger = MagicMock
    interval = _fake_module("apscheduler.triggers.interval")
    interval.IntervalTrigger = MagicMock


# --- shared fixtures --------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_storage_singleton():
    """Keep tests hermetic: core.storage caches a process-wide Storage singleton,
    so one test constructing it (e.g. with base_config's dummy Supabase URL)
    would leak a live client into every later test."""
    import core.storage as _storage_mod

    _storage_mod._storage = None
    yield
    _storage_mod._storage = None


@pytest.fixture
def base_config():
    """A ``Config`` populated with dummy credentials for every service."""
    from core.config import config

    return dataclasses.replace(
        config,
        anthropic_api_key="test-anthropic-key",
        google_api_key="test-google-key",
        heygen_api_key="test-heygen-key",
        heygen_voice_id="voice-123",
        heygen_avatar_id="avatar-123",
        supabase_url="https://test.supabase.co",
        # Supabase 2.15+ validates the key matches JWT dot-segment format.
        supabase_key="test.test.test",
        supabase_bucket="media",
        instagram_access_token="ig-token",
        instagram_business_account_id="ig-account",
        facebook_page_id="fb-page-123",
        twitter_api_key="tw-key",
        twitter_api_secret="tw-secret",
        twitter_access_token="tw-token",
        twitter_access_secret="tw-token-secret",
        linkedin_access_token="li-token",
        linkedin_author_urn="urn:li:person:123",
        tiktok_access_token="tt-token",
        dry_run=False,
    )
