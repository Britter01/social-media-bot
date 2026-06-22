"""Brite Tech Lifestyle — Automation Dashboard.

Styled to the Brite Tech Lifestyle brand kit (v2.0):
  • Palette  — White #FFFFFF, Off-White #F5F5F7, Smoke #E8E8ED, Silver #A1A1A6,
               Slate #6E6E73, Charcoal #1D1D1F, Black #000000, Brite Blue #0066CC.
  • Type     — Figtree (300–800) for UI; Playfair Display italic for the tagline.
  • Buttons  — always pill-shaped.
  • Logo     — the locked PNG wordmark, rendered from assets/logos.
Brite Blue is the only accent and is used sparingly.
"""

from __future__ import annotations

import base64
import calendar
import hmac
import html
import logging
import os
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from functools import lru_cache
from io import BytesIO

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)

# ── Brand palette ───────────────────────────────────────────────────────────────

WHITE = "#FFFFFF"
OFF_WHITE = "#F5F5F7"
SMOKE = "#E8E8ED"
SILVER = "#A1A1A6"
SLATE = "#6E6E73"
CHARCOAL = "#1D1D1F"
BLACK = "#000000"
ACCENT = "#0066CC"
ACCENT_LT = "#E8F0FA"

# Status semantics (kept restrained to suit the premium light aesthetic).
C_PENDING = "#B25E09"  # amber  — awaiting your review
C_PROGRESS = "#6E6E73"  # slate  — working
C_SCHEDULED = "#0066CC"  # blue   — queued
C_PUBLISHED = "#1D7A34"  # green  — live
C_FAILED = "#C4314B"  # red    — needs attention

# ── Brand logo (locked PNG → tightly-cropped data URI) ──────────────────────────

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Black wordmark (Brite in charcoal, TECH LIFESTYLE in slate) for the light UI.
_LOGO_FILE = os.path.join(_REPO_ROOT, "assets", "logos", "final_logo_transparent_black.png")


@lru_cache(maxsize=2)
def _logo_data_uri(path: str = _LOGO_FILE) -> str:
    """Load the locked logo PNG, crop to its content box, return a data URI."""
    try:
        from PIL import Image

        im = Image.open(path).convert("RGBA")
        bbox = im.getbbox()
        if bbox:
            left, top, right, bottom = bbox
            pad = 14
            im = im.crop(
                (
                    max(left - pad, 0),
                    max(top - pad, 0),
                    min(right + pad, im.width),
                    min(bottom + pad, im.height),
                )
            )
        buf = BytesIO()
        im.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        logger.warning("Could not load brand logo for the dashboard", exc_info=True)
        return ""


def _logo_html(width: int = 160) -> str:
    """Return an <img> for the locked logo, or a CSS wordmark fallback."""
    uri = _logo_data_uri()
    if uri:
        return (
            f'<img src="{uri}" alt="Brite Tech Lifestyle" '
            f'style="width:{width}px;max-width:80%;height:auto;display:inline-block" />'
        )
    return (
        "<div style=\"font-family:'Figtree',sans-serif;font-size:42px;font-weight:800;"
        f'letter-spacing:-0.04em;color:{CHARCOAL};line-height:1">Brite</div>'
        '<div style="font-family:Arial,sans-serif;font-size:9px;letter-spacing:0.28em;'
        f'color:{SLATE};text-transform:uppercase;margin-top:5px">Tech Lifestyle</div>'
    )


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Brite Tech Lifestyle — Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS / JS injection ────────────────────────────────────────────────────────
# Injected into the parent document (not the iframe) so it applies globally.
# We also start the 60-second auto-refresh countdown here.

components.html(
    r"""
<script>
(function () {
  const doc = window.parent.document;

  /* ── Brand fonts (Figtree + Playfair Display italic) ──────────────────── */
  if (!doc.getElementById('btl-fonts')) {
    const link = doc.createElement('link');
    link.id   = 'btl-fonts';
    link.rel  = 'stylesheet';
    link.href = 'https://fonts.googleapis.com/css2?family=Figtree:wght@300;400;500;600;700;800&family=Playfair+Display:ital,wght@1,400;1,500&display=swap';
    doc.head.appendChild(link);
  }

  /* ── Theme CSS (Brite brand kit — light) ─────────────────────────────── */
  const CSS = `
    :root {
      --white:#FFFFFF; --off-white:#F5F5F7; --smoke:#E8E8ED; --silver:#A1A1A6;
      --slate:#6E6E73; --charcoal:#1D1D1F; --black:#000000;
      --accent:#0066CC; --accent-lt:#E8F0FA;
    }
    html, body, [class*="css"], button, input, textarea, select {
      font-family:'Figtree',-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Arial,sans-serif !important;
    }
    .stApp, [data-testid="stAppViewContainer"] { background: var(--white) !important; }
    [data-testid="stHeader"] {
      background: var(--white) !important;
      border-bottom: 1px solid var(--smoke) !important;
    }
    [data-testid="stSidebar"] {
      background: var(--off-white) !important;
      border-right: 1px solid var(--smoke) !important;
    }
    [data-testid="stSidebar"] * { color: var(--charcoal) !important; }
    [data-testid="stSidebarContent"] h1,
    [data-testid="stSidebarContent"] h2,
    [data-testid="stSidebarContent"] h3 {
      font-family:'Figtree',sans-serif !important;
      font-weight:700 !important; letter-spacing:-0.02em !important;
    }
    #MainMenu, footer { visibility: hidden; }
    .block-container { padding-top: 3.5rem !important; max-width: 1420px !important; }

    h1, h2, h3 {
      font-family:'Figtree',sans-serif !important;
      font-weight:700 !important; letter-spacing:-0.02em !important;
      color: var(--charcoal) !important;
    }

    /* ── Hide Streamlit's deploy / manage-app toolbar (all known selectors) ── */
    [data-testid="stToolbar"],
    [data-testid="stStatusWidget"],
    [data-testid="stToolbarActions"],
    [data-testid="manage-app-button"],
    .stDeployButton,
    .viewerBadge_container__1QSob,
    .viewerBadge_link__qRIco,
    #stDecoration,
    footer,
    footer * { display: none !important; }

    /* ── Sidebar: always visible on desktop, hidden on mobile ───────────────
         On desktop (≥768px) we override the transform Streamlit uses to slide
         the sidebar off-screen so it stays locked open.  On mobile the sidebar
         is hidden entirely — the "Pipeline controls" expander in the main body
         provides the same buttons on small screens. */
    @media (min-width: 768px) {
      [data-testid="stSidebar"] {
        transform: none !important;
        min-width: 244px !important;
        visibility: visible !important;
        display: flex !important;
      }
      /* Hide the collapse button — sidebar is always open on desktop */
      [data-testid="stSidebarCollapseButton"],
      [data-testid="stSidebarCollapsedControl"],
      [data-testid="collapsedControl"],
      [data-testid="stExpandSidebarButton"] { display: none !important; }
    }
    @media (max-width: 767px) {
      [data-testid="stSidebar"] { display: none !important; }
    }

    /* Logo in the main header is shown only on mobile (the sidebar logo is
       hidden there). Hidden on desktop, where the sidebar logo is visible. */
    .btl-mobile-logo { display: none; }
    @media (max-width: 767px) {
      .btl-mobile-logo { display: block !important; }
    }

    /* ── Mobile tweaks (do NOT touch column wrapping — Streamlit needs it
          to stack columns vertically on small screens) ── */
    @media (max-width: 768px) {
      .block-container {
        padding-top: 3rem !important;
        padding-left: 0.75rem !important;
        padding-right: 0.75rem !important;
      }
      .stButton > button { min-height: 44px !important; font-size: 14px !important; }
    }

    /* ── Tabs (pill rail) ── */
    .stTabs [data-baseweb="tab-list"] {
      background: var(--off-white) !important;
      border: 1px solid var(--smoke) !important;
      border-radius: 980px !important;
      padding: 4px !important; gap: 2px !important; margin-bottom: 8px !important;
    }
    .stTabs [data-baseweb="tab"] {
      border-radius: 980px !important;
      font-weight: 600 !important; font-size: 13px !important;
      color: var(--slate) !important; background: transparent !important;
      padding: 7px 18px !important;
    }
    /* selected tab: set colour on the button AND every child element inside it */
    .stTabs [aria-selected="true"],
    .stTabs [aria-selected="true"] p,
    .stTabs [aria-selected="true"] span,
    .stTabs [aria-selected="true"] div {
      background: var(--charcoal) !important; color: var(--white) !important;
    }
    .stTabs [data-baseweb="tab-border"] { display: none !important; }
    .stTabs [data-baseweb="tab-panel"] { padding-top: 8px !important; }

    /* ── Buttons (always pill) — covers both regular and form-submit buttons ── */
    .stButton > button,
    [data-testid="stFormSubmitButton"] > button {
      border-radius: 980px !important; font-weight: 600 !important;
      background: var(--white) !important; color: var(--charcoal) !important;
      border: 1px solid var(--smoke) !important;
      transition: border-color .2s, background .2s !important;
    }
    .stButton > button:hover,
    [data-testid="stFormSubmitButton"] > button:hover {
      background: var(--off-white) !important;
      border-color: var(--charcoal) !important; color: var(--charcoal) !important;
    }
    /* primary button: force white text on every child element, incl. sidebar */
    .stButton > button[kind="primary"],
    .stButton > button[kind="primary"] p,
    .stButton > button[kind="primary"] span,
    .stButton > button[kind="primary"] div,
    [data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"],
    [data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"] p,
    [data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"] span,
    [data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"] div {
      background: var(--black) !important; border-color: var(--black) !important;
      color: var(--white) !important;
    }
    .stButton > button[kind="primary"]:hover,
    .stButton > button[kind="primary"]:hover p,
    .stButton > button[kind="primary"]:hover span,
    [data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"]:hover,
    [data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"]:hover p,
    [data-testid="stFormSubmitButton"] > button[kind="primaryFormSubmit"]:hover span {
      background: var(--charcoal) !important;
    }


    /* ── Containers ── */
    [data-testid="stVerticalBlockBorderWrapper"] {
      background: var(--white) !important;
      border: 1px solid var(--smoke) !important;
      border-radius: 18px !important;
    }

    /* ── Expanders ── */
    [data-testid="stExpander"] details {
      background: var(--white) !important;
      border: 1px solid var(--smoke) !important;
      border-radius: 14px !important; margin-bottom: 6px !important; overflow: hidden !important;
    }
    [data-testid="stExpander"] summary {
      background: var(--off-white) !important; color: var(--charcoal) !important;
      font-weight: 600 !important; font-size: 13px !important;
      padding: 10px 16px !important; cursor: pointer !important;
    }

    /* ── Alerts ── */
    .stAlert {
      background: var(--off-white) !important;
      border: 1px solid var(--smoke) !important;
      color: var(--charcoal) !important; border-radius: 14px !important;
    }

    /* ── Inputs ── */
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input {
      background: var(--white) !important;
      border: 1px solid var(--smoke) !important;
      color: var(--charcoal) !important; border-radius: 10px !important;
    }
    /* Keep typed text clear of the password show/hide (eye) button at the right
       of the field. Padding goes on the <input> so the dots stop short of the
       icon whether the field is masked (type=password) or revealed (type=text). */
    [data-testid="stTextInput"] input {
      padding-right: 2.75rem !important;
    }
    /* The "Press Enter to submit form" helper is an absolutely-positioned
       overlay across the right of the field — it sits over the dots next to the
       eye icon, which is what reads as text "running over" it. Hide it. */
    [data-testid="stTextInput"] [data-testid="InputInstructions"] {
      display: none !important;
    }
    [data-testid="stTextInput"] input:focus,
    [data-testid="stNumberInput"] input:focus { border-color: var(--accent) !important; }
    [data-baseweb="select"] > div:first-child {
      background: var(--white) !important;
      border: 1px solid var(--smoke) !important;
      color: var(--charcoal) !important; border-radius: 10px !important;
    }
    [data-baseweb="popover"] [data-baseweb="menu"] {
      background: var(--white) !important;
      border: 1px solid var(--smoke) !important; border-radius: 10px !important;
    }
    [data-baseweb="option"] { background: var(--white) !important; color: var(--charcoal) !important; }
    [data-baseweb="option"]:hover { background: var(--off-white) !important; }


    /* ── Image fullscreen toolbar ──
       DOM (confirmed via inspector):
         stFullScreenFrame > div.e1plw2qp2 > [stElementToolbar, stImage]
       Make the direct child wrapper (not stFullScreenFrame itself — changing
       its position breaks the fullscreen expand calculation) the containing
       block for stElementToolbar and pin it to the top-right corner. */
    [data-testid="stFullScreenFrame"] > div {
      position: relative !important;
    }
    [data-testid="stElementToolbar"] {
      position: absolute !important;
      top: 0.5rem !important;
      right: 0.5rem !important;
      bottom: auto !important;
      left: auto !important;
      z-index: 10 !important;
      width: auto !important;
    }

    /* ── Text ── */
    [data-testid="stMarkdownContainer"] p { color: var(--charcoal) !important; }
    [data-testid="stCaptionContainer"] { color: var(--slate) !important; }
    .stCaption { color: var(--slate) !important; }
    hr { border-color: var(--smoke) !important; }
    [data-testid="stMetric"] label { color: var(--slate) !important; }
    [data-testid="stMetricValue"] { color: var(--charcoal) !important; }
    [data-testid="stCheckbox"] label { color: var(--charcoal) !important; }
  `;

  if (!doc.getElementById('btl-css')) {
    const style = doc.createElement('style');
    style.id = 'btl-css';
    style.textContent = CSS;
    doc.head.appendChild(style);
  } else {
    doc.getElementById('btl-css').textContent = CSS;
  }

  /* ── Keep scroll position and the active tab across reruns ─────────────────
        Streamlit reruns the whole script on every button click, which resets
        the scroll position to the top AND snaps tab selection back to the
        first tab. We persist both in the parent page's sessionStorage and
        restore them once after each rerun. */
  const ss = window.parent.sessionStorage;
  const SK = 'btl-scrollTop';
  const TK = 'btl-activeTab';

  function scroller() {
    return doc.querySelector('[data-testid="stMain"]')
        || doc.querySelector('section.main')
        || doc.scrollingElement
        || doc.documentElement;
  }

  let scrollDone = false;
  let tabDone = false;

  function wire() {
    /* Scroll: remember position on scroll, restore once per rerun. */
    const sc = scroller();
    if (sc) {
      if (!sc.dataset.btlScrollWired) {
        sc.dataset.btlScrollWired = '1';
        sc.addEventListener(
          'scroll',
          () => { ss.setItem(SK, String(sc.scrollTop)); },
          { passive: true }
        );
      }
      if (!scrollDone) {
        scrollDone = true;
        const sv = ss.getItem(SK);
        const y = sv !== null ? parseInt(sv, 10) : NaN;
        if (!isNaN(y) && y > 0) {
          [120, 300, 600].forEach((d) =>
            setTimeout(() => { try { sc.scrollTop = y; } catch (e) {} }, d)
          );
        }
      }
    }

    /* Tabs: remember the selected tab, re-select it on rerun.
       We keep retrying on each poll tick rather than giving up after one
       click, because React can re-render the tab rail back to index 0
       after our simulated click.  We only stop when the correct tab is
       visibly selected, or when the user manually clicks a tab. */
    const list = doc.querySelector('.stTabs [data-baseweb="tab-list"]');
    if (list) {
      const tabs = list.querySelectorAll('[data-baseweb="tab"]');
      if (tabs.length) {
        if (!list.dataset.btlTabWired) {
          list.dataset.btlTabWired = '1';
          tabs.forEach((t, i) =>
            /* Only a *real* user click (isTrusted) saves the index and stops
               the restore loop. A scripted .click() below is NOT trusted, so
               it won't prematurely set tabDone — letting us keep retrying
               until React actually settles on the right tab. */
            t.addEventListener('click', (ev) => {
              if (ev.isTrusted) { ss.setItem(TK, String(i)); tabDone = true; }
            })
          );
        }
        if (!tabDone) {
          const want = ss.getItem(TK);
          const idx = want !== null ? parseInt(want, 10) : NaN;
          if (!isNaN(idx) && idx >= 0 && idx < tabs.length) {
            const cur = list.querySelector('[aria-selected="true"]');
            const ci  = Array.prototype.indexOf.call(tabs, cur);
            if (ci === idx) {
              tabDone = true;   /* already on the right tab — stop */
            } else {
              tabs[idx].click(); /* keep retrying each poll tick */
            }
          } else {
            tabDone = true;     /* no valid target */
          }
        }
      }
    }
  }

  /* Poll briefly because Streamlit renders the tab rail asynchronously. */
  let tries = 0;
  const iv = setInterval(() => {
    wire();
    if ((scrollDone && tabDone) || ++tries > 25) clearInterval(iv);
  }, 120);
  wire();


})();
</script>
""",
    height=0,
)

# ── Authentication ────────────────────────────────────────────────────────────

_AUTH_MAX_ATTEMPTS = 5
_AUTH_LOCKOUT_SECS = 900


def _check_password() -> bool:
    try:
        expected = st.secrets.get("DASHBOARD_PASSWORD") or os.getenv("DASHBOARD_PASSWORD", "")
    except Exception:
        expected = os.getenv("DASHBOARD_PASSWORD", "")
    if not expected:
        st.error("DASHBOARD_PASSWORD is not set.")
        st.stop()
    if st.session_state.get("authenticated"):
        return True
    now = datetime.now(UTC).timestamp()
    attempts: list[float] = [
        t for t in st.session_state.get("_auth_attempts", []) if now - t < _AUTH_LOCKOUT_SECS
    ]
    if len(attempts) >= _AUTH_MAX_ATTEMPTS:
        st.error("Too many failed attempts. Please wait 15 minutes and try again.")
        return False

    st.markdown("<br>" * 4, unsafe_allow_html=True)
    col = st.columns([1, 1, 1])[1]
    with col:
        st.markdown(
            f"""
<div style="text-align:center;margin-bottom:30px">
  {_logo_html(210)}
  <div style="font-family:'Playfair Display',serif;font-style:italic;font-size:15px;
              color:{SLATE};margin-top:18px">Technology, beautifully lived.</div>
  <div style="font-size:12px;color:{SILVER};margin-top:8px;letter-spacing:0.04em">
    Automation Dashboard</div>
</div>
""",
            unsafe_allow_html=True,
        )
        with st.form("login_form", border=False):
            pwd = st.text_input(
                "Password",
                type="password",
                placeholder="Enter password",
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button("Sign in", use_container_width=True, type="primary")
        if submitted:
            if hmac.compare_digest(pwd, expected):
                st.session_state["authenticated"] = True
                st.session_state["_auth_attempts"] = []
                st.rerun()
            else:
                attempts.append(now)
                st.session_state["_auth_attempts"] = attempts
                time.sleep(1)
                st.error("Incorrect password.")
    return False


if not _check_password():
    st.stop()

# ── Supabase ──────────────────────────────────────────────────────────────────


@st.cache_resource
def get_db():
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
    except (KeyError, FileNotFoundError):
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        st.error("SUPABASE_URL and SUPABASE_KEY must be set.")
        st.stop()
    return create_client(url, key)


db = get_db()


def _supabase_sql_editor_url() -> str:
    """Build a direct link to the Supabase SQL Editor for this project."""
    try:
        raw_url = st.secrets.get("SUPABASE_URL") or os.getenv("SUPABASE_URL", "")
        ref = raw_url.split("//")[-1].split(".")[0]
        if ref:
            return f"https://supabase.com/dashboard/project/{ref}/sql/new"
    except Exception:
        pass
    return "https://supabase.com/dashboard"


# ── Data ──────────────────────────────────────────────────────────────────────


@st.cache_data(ttl=60)
def load_topics():
    return (
        db.table("topics").select("*").order("relevance_score", desc=True).limit(200).execute().data
        or []
    )


@st.cache_data(ttl=60)
def load_posts():
    return (
        db.table("posts").select("*").order("scheduled_time", desc=False).limit(500).execute().data
        or []
    )


topics = load_topics()
posts = load_posts()


@st.cache_data(ttl=60)
def load_analytics(_db):
    """Return (rows, error). error is a short string if the table is missing
    or unreadable, else None — so the UI can tell 'empty' from 'broken'."""
    try:
        result = _db.table("post_analytics").select("*").execute()
        return result.data or [], None
    except Exception as exc:
        return [], str(exc)


def load_last_command_status(_db, command: str, prefix: bool = False):
    """Most recent pipeline_commands row for *command*, or None.

    When *prefix* is True, matches any command that starts with *command*
    (useful for commands that may carry a ``|topic`` suffix).
    """
    try:
        q = _db.table("pipeline_commands").select("*")
        q = q.like("command", f"{command}%") if prefix else q.eq("command", command)
        result = q.order("requested_at", desc=True).limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def _get_automation_state(_db) -> tuple[bool, str | None]:
    """Return (is_paused, since_str).

    Checks the most recent completed pause_automation / resume_automation
    command. If the latest is a pause, automation is paused. *since_str* is
    a human-readable timestamp of when the state last changed, or None.
    """
    try:
        result = (
            _db.table("pipeline_commands")
            .select("command, finished_at")
            .in_("command", ["pause_automation", "resume_automation"])
            .eq("status", "done")
            .order("finished_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return False, None
        row = rows[0]
        paused = row["command"] == "pause_automation"
        raw_ts = row.get("finished_at") or ""
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            since = dt.strftime("%d %b %Y · %H:%M UTC")
        except Exception:
            since = raw_ts or None
        return paused, since
    except Exception:
        return False, None


def _get_instagram_mode(_db) -> tuple[bool, str | None]:
    """Return (is_api_mode, since_str).

    Checks the most recent completed instagram_api_mode / instagram_telegram_mode
    command. Default (no commands yet) = Telegram mode.
    """
    try:
        result = (
            _db.table("pipeline_commands")
            .select("command, finished_at")
            .in_("command", ["instagram_api_mode", "instagram_telegram_mode"])
            .eq("status", "done")
            .order("finished_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return False, None
        row = rows[0]
        api_mode = row["command"] == "instagram_api_mode"
        raw_ts = row.get("finished_at") or ""
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            since = dt.strftime("%d %b %Y · %H:%M UTC")
        except Exception:
            since = raw_ts or None
        return api_mode, since
    except Exception:
        return False, None


analytics_rows, analytics_error = load_analytics(db)
# Build lookup: post_id -> best snapshot (prefer 7d over 24h)
analytics_by_post: dict[str, dict] = {}
for _arow in analytics_rows:
    _pid = _arow["post_id"]
    _existing = analytics_by_post.get(_pid)
    if _existing is None or _arow["snapshot_type"] == "7d":
        analytics_by_post[_pid] = _arow


def by_status(items, *statuses):
    return [i for i in items if i.get("status") in statuses]


pending = by_status(topics, "pending_approval")
approved_t = by_status(topics, "approved")
in_progress = by_status(posts, "content_ready", "media_ready")
scheduled = by_status(posts, "scheduled")
generated = by_status(posts, "manual_ready")
published = by_status(posts, "published")
failed = by_status(posts, "failed")

# ── Sidebar — pipeline controls ───────────────────────────────────────────────

_CMD_COOLDOWN_SECS = 10
_VALID_PILLARS = ["AI Guide", "Tech Lifestyle", "Productivity", "Fitness Tech", "Review"]
_VALID_PLATFORMS = ["instagram", "facebook", "twitter", "linkedin", "youtube", "tiktok"]


def _queue_command(command: str, cooldown_key: str | None = None) -> None:
    key = f"_cmd_ts_{cooldown_key or command}"
    now = datetime.now(UTC).timestamp()
    if now - st.session_state.get(key, 0.0) < _CMD_COOLDOWN_SECS:
        raise RuntimeError("Please wait a moment before running again.")
    db.table("pipeline_commands").insert(
        {"command": command, "status": "pending", "requested_at": datetime.now(UTC).isoformat()}
    ).execute()
    st.session_state[key] = now


# Command buttons shown in both the sidebar (desktop) and the main-body
# controls panel (reliable on mobile, where the sidebar is hidden behind a chevron).
_cmds = [
    (
        "Daily Research",
        "research",
        "Scans the web for trending topics and adds them to your approval queue.",
    ),
    (
        "Competitor Analysis",
        "weekly_strategy",
        "Runs the Monday competitor-pattern study right now — no need to wait. "
        "Queues 7 shaped content ideas for your approval.",
    ),
    ("Generate Posts", "content", "Creates posts from topics you have already approved."),
    ("Refresh Images", "image_refresh", "Regenerates any missing or failed thumbnails."),
    ("Publish Due Posts", "publish", "Sends any scheduled post whose time has passed."),
]


def _render_pipeline_controls(scope: str) -> None:
    """Render the pipeline command buttons. ``scope`` keeps widget keys unique
    so the same controls can appear in the sidebar and the main body."""
    # ── Master kill-switch ───────────────────────────────────────────────────
    _auto_paused, _auto_since = _get_automation_state(db)
    if _auto_paused:
        st.markdown(
            f"<div style='background:#FFF3CD;border:1px solid #F0AD4E;border-radius:12px;"
            f"padding:10px 14px;margin-bottom:8px;font-size:13px;font-weight:600;"
            f"color:#7B4F00'>⚠️ AUTOMATION PAUSED{f' since {_auto_since}' if _auto_since else ''}"
            f"</div>",
            unsafe_allow_html=True,
        )
        if st.button(
            "▶️  Resume Automation",
            use_container_width=True,
            type="primary",
            key=f"{scope}_resume_auto",
            help="Re-enables all scheduled and command-queue jobs.",
        ):
            try:
                _queue_command("resume_automation", cooldown_key="automation_switch")
                st.success("Resume queued — automation restarts within ~2 min.")
            except RuntimeError:
                pass
            except Exception:
                st.error("Failed to queue resume.")
    else:
        if st.button(
            "🛑  Pause All Automation",
            use_container_width=True,
            key=f"{scope}_pause_auto",
            help=(
                "Master kill-switch: stops all scheduled jobs and command-queue execution "
                "(research, content generation, image generation, publishing). Nothing will "
                "post or call any paid API until you press Resume. Any job currently running "
                "will finish naturally — no new jobs start after."
            ),
        ):
            try:
                _queue_command("pause_automation", cooldown_key="automation_switch")
                st.warning("Pause queued — automation stops within ~2 min.")
            except RuntimeError:
                pass
            except Exception:
                st.error("Failed to queue pause.")

    st.markdown(
        f"<div style='border-top:1px solid {SMOKE};margin:8px 0 10px'></div>",
        unsafe_allow_html=True,
    )
    # ── Regular pipeline controls ────────────────────────────────────────────
    for label, cmd, tip in _cmds:
        if st.button(label, use_container_width=True, help=tip, key=f"{scope}_{cmd}"):
            try:
                _queue_command(cmd)
            except RuntimeError:
                pass
            except Exception:
                st.error("Failed to queue command.")

    st.markdown(
        f"<div style='border-top:1px solid {SMOKE};margin:10px 0 8px'></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div style='font-family:Figtree,sans-serif;font-size:10px;font-weight:600;"
        f"letter-spacing:0.16em;text-transform:uppercase;color:{SILVER};margin-bottom:6px'>"
        "Quick action</div>",
        unsafe_allow_html=True,
    )

    if st.button(
        "Research + Generate",
        use_container_width=True,
        help=(
            "Runs Competitor Analysis AND generates posts from already-approved topics "
            "in one go. New topics from the research still need your approval before "
            "the next Generate run picks them up."
        ),
        key=f"{scope}_research_generate",
    ):
        try:
            _queue_command("weekly_strategy", cooldown_key="rg_strategy")
            _queue_command("content", cooldown_key="rg_content")
        except RuntimeError:
            pass
        except Exception:
            st.error("Failed to queue commands.")

    if st.button(
        "🩺  System Check",
        use_container_width=True,
        help=(
            "Runs a self-test inside the worker: which API keys it can see, "
            "whether the image agents can start, and whether Supabase Storage "
            "is writable. Use this when posts come out with no image."
        ),
        key=f"{scope}_diagnostics",
    ):
        try:
            _queue_command("diagnostics", cooldown_key="diagnostics")
            st.info("System check queued — the result appears below within ~2 min.")
        except RuntimeError:
            pass
        except Exception:
            st.error("Failed to queue system check.")

    _diag = load_last_command_status(db, "diagnostics")
    if _diag:
        _diag_status = _diag.get("status", "")
        _diag_msg = _diag.get("error") or ""
        if _diag_status == "done" and _diag_msg:
            st.caption(f"🩺 Last system check: {_diag_msg}")
        elif _diag_status in ("pending", "running"):
            st.caption("🩺 System check running…")

    if st.button(
        "#️⃣  Trim hashtags to 5",
        use_container_width=True,
        help=(
            "Scans every scheduled post, pulls any hashtags written into the caption "
            "into the hashtags field, de-duplicates, and caps each post at 5 relevant "
            "hashtags. Captions are left as clean prose."
        ),
        key=f"{scope}_cleanup_hashtags",
    ):
        try:
            _queue_command("cleanup_hashtags", cooldown_key="cleanup_hashtags")
            st.info("Hashtag cleanup queued — the result appears below within ~2 min.")
        except RuntimeError:
            pass
        except Exception:
            st.error("Failed to queue hashtag cleanup.")

    _htc = load_last_command_status(db, "cleanup_hashtags")
    if _htc:
        _htc_status = _htc.get("status", "")
        _htc_msg = _htc.get("error") or ""
        if _htc_status == "done" and _htc_msg:
            st.caption(f"#️⃣ {_htc_msg}")
        elif _htc_status in ("pending", "running"):
            st.caption("#️⃣ Hashtag cleanup running…")
        elif _htc_msg:
            st.caption(f"#️⃣ Failed: {_htc_msg}")
        elif _diag_msg:
            st.caption(f"🩺 Last system check failed: {_diag_msg}")

    if st.button(
        "🔑  Refresh Meta Token",
        use_container_width=True,
        help=(
            "Re-exchanges the Facebook/Instagram long-lived token for a fresh "
            "~60-day one and stores it so publishing keeps working without a "
            "redeploy. Runs automatically every Sunday; use this to force it. "
            "Requires FACEBOOK_APP_ID and FACEBOOK_APP_SECRET in Railway."
        ),
        key=f"{scope}_refresh_token",
    ):
        try:
            _queue_command("refresh_token", cooldown_key="refresh_token")
            st.info("Token refresh queued — the result appears below within ~2 min.")
        except RuntimeError:
            pass
        except Exception:
            st.error("Failed to queue token refresh.")

    _tok = load_last_command_status(db, "refresh_token")
    if _tok:
        _tok_status = _tok.get("status", "")
        _tok_msg = _tok.get("error") or ""
        if _tok_status == "done" and _tok_msg:
            st.caption(f"🔑 Last token refresh: {_tok_msg}")
        elif _tok_status in ("pending", "running"):
            st.caption("🔑 Token refresh running…")
        elif _tok_msg:
            st.caption(f"🔑 Last token refresh failed: {_tok_msg}")

    st.markdown("---")
    st.markdown(
        "<div style='font-size:12px;font-weight:600;letter-spacing:0.1em;"
        "text-transform:uppercase;color:#888;margin-bottom:6px'>Generate Infographic</div>",
        unsafe_allow_html=True,
    )
    _infog_format = st.selectbox(
        "Format",
        [
            "Instagram + Facebook Reels",
            "Instagram Reel only",
            "Facebook Reel only",
            "Static Grid (Instagram)",
            "Wheel Style (Instagram)",
            "Dark Panels (Instagram)",
            "Light Magazine (Instagram)",
            "Rich Slide — Dark (IG+FB)",
            "Rich Slide — Light (IG+FB)",
        ],
        key=f"{scope}_infog_fmt",
        label_visibility="collapsed",
    )
    _infog_cmd_map = {
        "Instagram + Facebook Reels": "create_infographic",
        "Instagram Reel only": "create_infographic_ig",
        "Facebook Reel only": "create_infographic_fb",
        "Static Grid (Instagram)": "create_infographic_static",
        "Wheel Style (Instagram)": "create_infographic_wheel",
        "Dark Panels (Instagram)": "create_infographic_dark",
        "Light Magazine (Instagram)": "create_infographic_light",
        "Rich Slide — Dark (IG+FB)": "create_infographic_rich_dark",
        "Rich Slide — Light (IG+FB)": "create_infographic_rich_light",
    }
    # Topic / category selector
    _INFOG_TOPIC_MAP: dict[str, str | None] = {
        "Auto (daily rotation)": None,
        "AI Productivity Tools & ROI": ("AI productivity tools adoption and ROI statistics 2026"),
        "ChatGPT & Generative AI Business Use": (
            "ChatGPT and generative AI business usage statistics 2026"
        ),
        "AI Impact on Jobs & Salaries": (
            "AI impact on jobs: automation, new roles and salary statistics 2026"
        ),
        "AI Coding Assistants": ("AI coding assistants: developer productivity statistics 2026"),
        "AI Content Creation": ("AI content creation: usage and engagement statistics 2026"),
        "Smart Home & AI Assistants": (
            "Smart home devices and AI assistant growth statistics 2026"
        ),
        "Wearable Tech & Fitness AI": ("Wearable tech and AI fitness tracking statistics 2026"),
        "Remote Work Tech": ("Remote work tech and AI collaboration tools statistics 2026"),
        "AI in Healthcare": (
            "AI in healthcare: diagnosis accuracy and patient outcome statistics 2026"
        ),
        "AI in Education": (
            "AI in education: student learning outcomes and adoption statistics 2026"
        ),
        "AI in Finance": ("AI in finance: fraud detection and trading statistics 2026"),
        "AI in Cybersecurity": (
            "AI cybersecurity: threat detection and breach prevention statistics 2026"
        ),
        "AI Customer Service & Chatbots": (
            "AI customer service: chatbot adoption and satisfaction statistics 2026"
        ),
        "Generative AI Market & Investment": (
            "Generative AI market size and investment growth 2026"
        ),
        "Self-Driving & Autonomous Vehicles": (
            "Self-driving and autonomous vehicle technology statistics 2026"
        ),
        "✏️  Custom topic…": "CUSTOM",
    }
    _infog_topic_label = st.selectbox(
        "Topic",
        list(_INFOG_TOPIC_MAP.keys()),
        key=f"{scope}_infog_topic",
        label_visibility="collapsed",
    )
    _infog_topic_val = _INFOG_TOPIC_MAP[_infog_topic_label]
    if _infog_topic_val == "CUSTOM":
        _infog_topic_val = (
            st.text_input(
                "Custom topic",
                placeholder="e.g. AI in retail industry statistics 2026",
                key=f"{scope}_infog_custom",
                label_visibility="collapsed",
            ).strip()
            or None
        )

    if st.button(
        "📊  Generate Infographic",
        use_container_width=True,
        help=(
            "Research a trending AI/tech topic, compose 5 eye-catching stat cards using "
            "Higgsfield visuals, and assemble them into a 15-second Reel. "
            "The finished post is auto-scheduled. Takes ~2 minutes."
        ),
        key=f"{scope}_infog_btn",
    ):
        try:
            _cmd = _infog_cmd_map[_infog_format]
            if _infog_topic_val:
                _cmd = f"{_cmd}|{_infog_topic_val}"
            _queue_command(_cmd, cooldown_key="create_infographic")
            st.info("Infographic queued — the Reel appears in Scheduled within ~5 min.")
        except RuntimeError:
            pass
        except Exception:
            st.error("Failed to queue infographic.")

    _infog_last = load_last_command_status(db, "create_infographic", prefix=True)
    if _infog_last:
        _is = _infog_last.get("status", "")
        _im = _infog_last.get("error") or ""
        if _is in ("pending", "running"):
            st.caption("📊 Infographic generating…")
        elif _im and "spending limit" in _im.lower():
            st.warning(
                "⚠️ Anthropic API spending limit reached. "
                "Raise your cap at console.anthropic.com to resume."
            )
        elif _is == "done" and _im:
            st.caption(f"📊 {_im}")
        elif _im:
            st.caption(f"📊 Failed: {_im}")

    st.markdown("---")
    st.markdown(
        "<div style='font-size:12px;font-weight:600;letter-spacing:0.1em;"
        "text-transform:uppercase;color:#888;margin-bottom:6px'>AI News Carousel</div>",
        unsafe_allow_html=True,
    )
    if st.button(
        "📰  Generate AI News Now",
        use_container_width=True,
        help=(
            "Fetch today's top 3 AI news stories via web search and publish a "
            "5-slide branded carousel to Instagram + Facebook. Auto-runs daily at noon."
        ),
        key=f"{scope}_ai_news_btn",
    ):
        try:
            _queue_command("create_ai_news", cooldown_key="create_ai_news")
            st.info("AI News carousel queued — appears in Generated tab within ~3 min.")
        except RuntimeError:
            pass
        except Exception:
            st.error("Failed to queue AI news carousel.")

    _news_last = load_last_command_status(db, "create_ai_news")
    if _news_last:
        _ns = _news_last.get("status", "")
        _nm = _news_last.get("error") or ""
        if _ns in ("pending", "running"):
            st.caption("📰 AI news carousel generating…")
        elif _ns == "done" and _nm:
            st.caption(f"📰 {_nm}")
        elif _nm:
            st.caption(f"📰 Failed: {_nm}")

    if st.button(
        "🖼️  Regenerate news background",
        use_container_width=True,
        help=(
            "Clears the stored AI background template for the news carousel. "
            "The next carousel run (auto at noon or via Generate AI News Now) "
            "will produce a fresh Higgsfield/Imagen background and save it as "
            "the new template — no need to touch Supabase manually."
        ),
        key=f"{scope}_regen_news_bg",
    ):
        try:
            _queue_command("regen_news_bg", cooldown_key="regen_news_bg")
            st.info(
                "Background reset queued — fresh AI background generates on the next carousel run."
            )
        except RuntimeError:
            pass
        except Exception:
            st.error("Failed to queue background reset.")

    _regen_last = load_last_command_status(db, "regen_news_bg")
    if _regen_last:
        _rs = _regen_last.get("status", "")
        _rm = _regen_last.get("error") or ""
        if _rs in ("pending", "running"):
            st.caption("🖼️ Background reset running…")
        elif _rs == "done" and _rm:
            st.caption(f"🖼️ {_rm}")
        elif _rm:
            st.caption(f"🖼️ Failed: {_rm}")

    st.markdown("---")
    st.markdown(
        "<div style='font-size:12px;font-weight:600;letter-spacing:0.1em;"
        "text-transform:uppercase;color:#888;margin-bottom:6px'>Instagram Publishing</div>",
        unsafe_allow_html=True,
    )
    _ig_api_mode, _ig_since = _get_instagram_mode(db)
    if _ig_api_mode:
        st.markdown(
            "<div style='background:#E8F0FA;border:1px solid #0066CC;border-radius:12px;"
            "padding:8px 12px;margin-bottom:8px;font-size:12px;font-weight:600;"
            f"color:#003D7A'>📡 API mode{f' since {_ig_since}' if _ig_since else ''}</div>",
            unsafe_allow_html=True,
        )
        if st.button(
            "📱  Back to Telegram",
            use_container_width=True,
            type="primary",
            key=f"{scope}_ig_telegram",
            help="Route Instagram posts to Telegram again for manual native posting.",
        ):
            try:
                _queue_command("instagram_telegram_mode", cooldown_key="instagram_mode")
                st.success("Switching to Telegram mode — takes effect within ~2 min.")
            except RuntimeError:
                pass
            except Exception:
                st.error("Failed to queue mode switch.")
    else:
        st.markdown(
            "<div style='background:#E8F5E9;border:1px solid #A5D6A7;border-radius:12px;"
            "padding:8px 12px;margin-bottom:8px;font-size:12px;font-weight:600;"
            "color:#2E7D32'>📱 Telegram mode (manual posting)</div>",
            unsafe_allow_html=True,
        )
        if st.button(
            "📡  Switch to API publishing",
            use_container_width=True,
            key=f"{scope}_ig_api",
            help=(
                "Publish Instagram posts automatically via the Graph API. "
                "Use when you can't check Telegram. "
                "Organic reach may be lower than native posting."
            ),
        ):
            try:
                _queue_command("instagram_api_mode", cooldown_key="instagram_mode")
                st.info("Switching to API mode — Instagram posts will auto-publish within ~2 min.")
            except RuntimeError:
                pass
            except Exception:
                st.error("Failed to queue mode switch.")

    if st.button("↺  Refresh data now", use_container_width=True, key=f"{scope}_refresh"):
        st.cache_data.clear()
        st.rerun()


with st.sidebar:
    st.markdown(
        f"""
<div style="padding:12px 0 22px;text-align:center">
  {_logo_html(150)}
</div>
""",
        unsafe_allow_html=True,
    )

    st.markdown(
        "<div style='font-family:Figtree,sans-serif;font-size:11px;font-weight:600;"
        f"letter-spacing:0.18em;text-transform:uppercase;color:{ACCENT};margin-bottom:8px'>"
        "Pipeline Controls</div>",
        unsafe_allow_html=True,
    )

    _render_pipeline_controls("sb")

    now_utc = datetime.now(UTC)
    st.markdown(
        f"<div style='font-size:11px;color:{SILVER};margin-top:12px'>"
        f"{now_utc.strftime('%d %b %Y · %H:%M UTC')}</div>",
        unsafe_allow_html=True,
    )

# ── Header ────────────────────────────────────────────────────────────────────

st.markdown(
    f"""
<div style="background:{OFF_WHITE};border:1px solid {SMOKE};border-radius:18px;
            padding:20px 30px;margin-bottom:16px;
            display:flex;align-items:center;justify-content:space-between">
  <div>
    <div class="btl-mobile-logo" style="margin-bottom:12px">{_logo_html(120)}</div>
    <div style="font-family:'Figtree',sans-serif;font-size:11px;
                font-weight:600;letter-spacing:0.18em;text-transform:uppercase;
                color:{ACCENT};margin-bottom:6px">Content Pipeline</div>
    <div style="font-family:'Figtree',sans-serif;font-size:28px;
                font-weight:700;letter-spacing:-0.02em;color:{CHARCOAL};line-height:1.05">
      Everything that goes out, in one place.
    </div>
  </div>
  <div style="text-align:right">
    <div style="font-family:'Playfair Display',serif;font-style:italic;font-size:14px;
                color:{SLATE}">Technology, beautifully lived.</div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

# ── Automation pause banner (shown prominently in main content when paused) ──────

_banner_paused, _banner_since = _get_automation_state(db)
if _banner_paused:
    st.markdown(
        f"""
<div style="background:#FFF3CD;border:2px solid #F0AD4E;border-radius:16px;
            padding:18px 24px;margin-bottom:12px;display:flex;
            align-items:center;justify-content:space-between;gap:16px">
  <div>
    <div style="font-family:'Figtree',sans-serif;font-size:16px;font-weight:700;
                color:#7B4F00">⚠️ Automation is paused</div>
    <div style="font-size:13px;color:#7B4F00;margin-top:4px">
      All scheduled jobs and pipeline commands are skipped until you resume.
      {f"Paused since {_banner_since}." if _banner_since else ""}
      Posts in the queue are held — nothing will post or generate.
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

# ── Pipeline controls (main body — always reachable, incl. mobile) ──────────────

with st.expander("⚙  Pipeline controls — run the bot manually", expanded=False):
    _render_pipeline_controls("main")

# ── Pipeline status bar ───────────────────────────────────────────────────────

STAGES = [
    ("Pending Review", len(pending), C_PENDING),
    ("In Progress", len(in_progress), C_PROGRESS),
    ("Scheduled", len(scheduled), C_SCHEDULED),
    ("Published", len(published), C_PUBLISHED),
    ("Failed", len(failed), C_FAILED),
]

_stage_cells = "".join(
    f"""<div style="flex:0 0 auto;min-width:110px;background:{OFF_WHITE};border:1px solid {SMOKE};
                   border-radius:14px;padding:16px 10px;text-align:center">
      <div style="font-size:34px;font-weight:700;font-family:'Figtree',sans-serif;
                  color:{color};line-height:1;letter-spacing:-0.02em">{count}</div>
      <div style="font-size:10px;font-weight:600;letter-spacing:0.1em;text-transform:uppercase;
                  color:{SLATE};margin-top:6px">{label}</div>
    </div>"""
    for label, count, color in STAGES
)
st.markdown(
    f"<div style='display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;margin-bottom:8px'>"
    f"{_stage_cells}</div>",
    unsafe_allow_html=True,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

PLATFORM_COLORS = {
    "instagram": "#E91E8C",
    "facebook": "#1877F2",
    "twitter": "#1DA1F2",
    "linkedin": "#0A66C2",
    "tiktok": "#25F4EE",
    "youtube": "#FF0000",
}

PLATFORM_ABBR = {
    "instagram": "IG",
    "facebook": "FB",
    "twitter": "TW",
    "linkedin": "LI",
    "tiktok": "TK",
    "youtube": "YT",
}


def _pill(text: str, color: str, bg_alpha: str = "18") -> str:
    e = html.escape(str(text))
    return (
        f"<span style='background:{color}{bg_alpha};color:{color};border-radius:980px;"
        f"padding:2px 11px;font-size:11px;font-weight:600;letter-spacing:0.04em;"
        f"text-transform:uppercase'>{e}</span>"
    )


def _sched_str(p):
    raw = p.get("scheduled_time") or p.get("published_time") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%a %d %b · %H:%M")
    except Exception:
        return raw


def _compact_num(n: int | None) -> str:
    """Format an integer compactly: 1200 → '1.2k', 1000000 → '1.0M'."""
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _model_badges(post: dict) -> str:
    """Return HTML model-attribution badges inferred from post type and meta."""
    post_type = post.get("post_type") or "standard"
    has_media = bool(post.get("thumbnail_url") or post.get("video_url"))
    bg_source = (post.get("meta") or {}).get("bg_source")

    image_model = "Higgsfield" if os.environ.get("HIGGSFIELD_API_KEY") else "Imagen 3"

    def _badge(label: str, bg: str, fg: str) -> str:
        return (
            f"<span style='background:{bg};color:{fg};font-size:10px;font-weight:600;"
            f"padding:2px 6px;border-radius:4px;white-space:nowrap'>{label}</span>"
        )

    parts = [_badge("Claude", "#EDE9FE", "#5B21B6")]

    if has_media:
        if bg_source == "cache":
            # Background served from Supabase cache — we don't know which API generated it.
            # After the v3 cache bump, new generations store "higgsfield" or "imagen_3" directly.
            parts.append(_badge("BG Cached", "#E0E7FF", "#3730A3"))
        elif bg_source == "higgsfield":
            parts.append(_badge("Higgsfield", "#DBEAFE", "#1D4ED8"))
        elif bg_source == "none":
            pass  # PIL-only styles with no image API
        else:
            # Freshly generated (no cache hit) — bg_source is "higgsfield" or "imagen_3"
            label = "Imagen 3" if bg_source in (None, "imagen_3") else image_model
            parts.append(_badge(label, "#DBEAFE", "#1D4ED8"))

    if post_type in ("reel", "infographic_reel"):
        parts.append(_badge("Freesound", "#D1FAE5", "#065F46"))

    return " ".join(parts)


def _post_card(
    post: dict, time_str: str = "", time_label: str = "", analytics: dict | None = None
) -> None:
    is_carousel = post.get("post_type") == "carousel"
    is_reel = post.get("post_type") in ("reel", "infographic_reel")
    _INFOGRAPHIC_TYPES = (
        "infographic_reel",
        "infographic_static",
        "infographic_wheel",
        "infographic_dark",
        "infographic_light",
        "infographic_rich_dark",
        "infographic_rich_light",
    )
    is_infographic = post.get("post_type") in _INFOGRAPHIC_TYPES
    slides = post.get("slides") or []
    video_url = post.get("video_url", "")
    platform = post.get("platform", "")
    title = post.get("title") or post.get("topic") or "Untitled"
    pillar = post.get("pillar") or "—"
    caption = post.get("caption") or ""
    hashtags = post.get("hashtags") or []
    plat_color = PLATFORM_COLORS.get(platform.lower(), SLATE)
    sched_color = C_SCHEDULED if time_label == "scheduled" else C_PUBLISHED

    e_title = html.escape(str(title))
    e_pillar = html.escape(str(pillar))
    e_caption = html.escape(str(caption))

    url = post.get("thumbnail_url", "")
    if is_reel and video_url:
        # Reels carry only a video — show the playable MP4 rather than an image.
        st.video(video_url)
    elif url:
        st.image(url if url.endswith(".png") else url + ".png", use_container_width=True)
    else:
        placeholder = "Rendering video…" if is_reel else "No image yet"
        st.markdown(
            f"<div style='background:{OFF_WHITE};border:1px solid {SMOKE};border-radius:12px;"
            "height:88px;display:flex;align-items:center;justify-content:center;"
            f"color:{SILVER};font-size:12px'>{placeholder}</div>",
            unsafe_allow_html=True,
        )

    pills = _pill(platform, plat_color) + " "
    if is_infographic:
        pills += (
            f"<span style='color:{SLATE};font-size:11px;font-weight:600'>📊 Infographic</span> "
        )
    elif is_reel:
        pills += f"<span style='color:{SLATE};font-size:11px;font-weight:600'>🎬 Reel</span> "
    if is_carousel:
        pills += (
            f"<span style='color:{SLATE};font-size:11px;font-weight:600'>"
            f"⬡ {len(slides)} slides</span> "
        )
    if time_str:
        icon = "📅" if time_label == "scheduled" else "📢"
        pills += (
            f"<span style='color:{sched_color};font-size:11px;font-weight:600'>"
            f"{icon} {html.escape(time_str)}</span>"
        )

    model_html = _model_badges(post)
    st.markdown(
        f"""<div style='padding:8px 2px 4px'>
          <div style='margin-bottom:4px'>{pills}</div>
          <div style='margin-bottom:6px;display:flex;gap:4px;flex-wrap:wrap'>{model_html}</div>
          <div style='font-family:"Figtree",sans-serif;font-size:17px;font-weight:700;
                      letter-spacing:-0.01em;color:{CHARCOAL};line-height:1.25;margin-bottom:3px'>
            {e_title}</div>
          <div style='font-size:11px;color:{SLATE}'>{e_pillar}</div>
        </div>""",
        unsafe_allow_html=True,
    )

    # Analytics metric badges
    if analytics:
        reach = analytics.get("reach") or analytics.get("impressions")
        likes = analytics.get("likes")
        comments = analytics.get("comments")
        badge_parts = []
        if reach is not None:
            badge_parts.append(f"👁 {_compact_num(reach)}")
        if likes is not None:
            badge_parts.append(f"❤ {_compact_num(likes)}")
        if comments is not None:
            badge_parts.append(f"💬 {_compact_num(comments)}")
        if badge_parts:
            badges_html = "  ".join(badge_parts)
            st.markdown(
                f"<div style='font-size:12px;color:{SLATE};padding:2px 2px 4px'>{badges_html}</div>",
                unsafe_allow_html=True,
            )

    # Expandable caption / slides
    if is_carousel and slides:
        with st.expander(f"View {len(slides)} slides"):
            for j, slide in enumerate(slides):
                role = slide.get("role", "")
                tag = " (cover)" if role == "cover" else " (CTA)" if role == "cta" else ""
                e_hl = html.escape(str(slide.get("headline", "")))
                e_bd = html.escape(str(slide.get("body", "")))
                st.markdown(
                    f"<div style='font-weight:700;color:{CHARCOAL};font-size:13px'>"
                    f"{j + 1}. {e_hl}{tag}</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"<div style='font-size:12px;color:{SLATE};margin-bottom:6px'>{e_bd}</div>",
                    unsafe_allow_html=True,
                )
                img = slide.get("image_url", "")
                if img:
                    st.image(
                        img if img.endswith(".png") else img + ".png", use_container_width=True
                    )
                st.divider()
    elif caption:
        tags_html = (
            f"<div style='font-size:11px;color:{ACCENT};margin-top:6px;line-height:1.8'>"
            + " ".join(f"#{html.escape(str(h))}" for h in hashtags)
            + "</div>"
            if hashtags
            else ""
        )
        st.markdown(
            f"""<details style="margin-top:8px;border:1px solid {SMOKE};border-radius:14px;overflow:hidden">
              <summary style="padding:9px 14px;font-size:12px;font-weight:600;color:{CHARCOAL};
                              background:{OFF_WHITE};cursor:pointer;list-style:none">
                Caption ›
              </summary>
              <div style="padding:12px 14px;font-size:13px;color:{CHARCOAL};
                          line-height:1.7;background:{WHITE}">
                {e_caption}{tags_html}
              </div>
            </details>""",
            unsafe_allow_html=True,
        )


# ── Tabs ──────────────────────────────────────────────────────────────────────

(
    tab_topics,
    tab_progress,
    tab_scheduled,
    tab_generated,
    tab_calendar,
    tab_published,
    tab_analytics,
    tab_pipeline,
) = st.tabs(
    [
        "Topics",
        "In Progress",
        "Scheduled",
        f"Generated{f' ({len(generated)})' if generated else ''}",
        "Calendar",
        "Published",
        "Analytics",
        "Pipeline",
    ]
)
# Alias for backward compatibility with any remaining references
tab_posts = tab_progress

# ── Topics ────────────────────────────────────────────────────────────────────

with tab_topics:
    if not pending:
        st.info("All clear — no topics awaiting review. Research runs daily at 05:30.")
    else:
        # Bulk approve
        ba_col, _, count_col = st.columns([1, 3, 1])
        with ba_col:
            if st.button(f"Approve all {len(pending)}", type="primary"):
                ids = [t["id"] for t in pending if t.get("id")]
                if ids:
                    db.table("topics").update({"status": "approved"}).in_("id", ids).execute()
                    st.cache_data.clear()
                    st.rerun()
        with count_col:
            st.markdown(
                f"<div style='text-align:right;font-size:12px;color:{SLATE};padding-top:6px'>"
                f"{len(pending)} awaiting</div>",
                unsafe_allow_html=True,
            )

        for topic in pending:
            with st.container(border=True):
                score = topic.get("relevance_score", 0)
                sc = C_PUBLISHED if score >= 80 else C_PENDING if score >= 60 else C_FAILED
                tid = topic["id"]
                plat = topic.get("platform", "")
                e_title = html.escape(str(topic.get("title", "")))
                pillar_val = topic.get("pillar", "—")

                # Header row: title + score + platform + action buttons
                h_left, h_right = st.columns([5, 1])
                with h_left:
                    st.markdown(
                        f'<div style=\'font-family:"Figtree",sans-serif;'
                        f"font-size:18px;font-weight:700;letter-spacing:-0.01em;"
                        f"color:{CHARCOAL};margin-bottom:5px'>"
                        f"{e_title}</div>"
                        f"<div style='margin-bottom:6px'>"
                        f"{_pill(f'Score {score}', sc)} &nbsp;"
                        f"{_pill(plat, PLATFORM_COLORS.get(plat.lower(), SLATE))} &nbsp;"
                        f"<span style='font-size:11px;color:{SLATE}'>{html.escape(pillar_val)}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(topic.get("summary", ""))
                    if topic.get("content_angle"):
                        st.markdown(
                            f"<div style='font-size:12px;color:{SLATE};margin-top:2px'>"
                            f"<b style='color:{CHARCOAL}'>Angle:</b> "
                            f"{html.escape(topic['content_angle'])}</div>",
                            unsafe_allow_html=True,
                        )
                    if topic.get("rationale"):
                        st.markdown(
                            f"<div style='font-size:12px;color:{SLATE}'>"
                            f"<b style='color:{CHARCOAL}'>Why:</b> "
                            f"{html.escape(topic['rationale'])}</div>",
                            unsafe_allow_html=True,
                        )
                    for src in (topic.get("sources") or [])[:2]:
                        if src.startswith(("http://", "https://")):
                            st.markdown(
                                f"<div style='font-size:11px;color:{ACCENT}'>"
                                f"<a href='{html.escape(src)}' target='_blank' "
                                f"style='color:{ACCENT}'>{html.escape(src[:60])}…</a></div>",
                                unsafe_allow_html=True,
                            )

                with h_right:
                    if st.button(
                        "Approve", key=f"a_{tid}", use_container_width=True, type="primary"
                    ):
                        db.table("topics").update({"status": "approved"}).eq("id", tid).execute()
                        st.cache_data.clear()
                        st.rerun()
                    if st.button("Reject", key=f"r_{tid}", use_container_width=True):
                        db.table("topics").update({"status": "rejected"}).eq("id", tid).execute()
                        st.cache_data.clear()
                        st.rerun()

                # Edit platform / pillar inline
                with st.expander("Edit before approving"):
                    ef1, ef2, ef3 = st.columns([2, 2, 1])
                    with ef1:
                        new_platform = st.selectbox(
                            "Platform",
                            _VALID_PLATFORMS,
                            index=_VALID_PLATFORMS.index(plat) if plat in _VALID_PLATFORMS else 0,
                            key=f"edit_plat_{tid}",
                        )
                    with ef2:
                        new_pillar = st.selectbox(
                            "Pillar",
                            _VALID_PILLARS,
                            index=_VALID_PILLARS.index(pillar_val)
                            if pillar_val in _VALID_PILLARS
                            else 0,
                            key=f"edit_pillar_{tid}",
                        )
                    with ef3:
                        st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                        if st.button("Save & Approve", key=f"save_{tid}", use_container_width=True):
                            db.table("topics").update(
                                {
                                    "platform": new_platform,
                                    "pillar": new_pillar,
                                    "status": "approved",
                                }
                            ).eq("id", tid).execute()
                            st.cache_data.clear()
                            st.rerun()

# ── In Progress ───────────────────────────────────────────────────────────────

with tab_progress:
    if not in_progress:
        st.info("Nothing being processed right now.")
    else:
        cols = st.columns(3)
        for i, post in enumerate(in_progress):
            with cols[i % 3]:
                with st.container(border=True):
                    _post_card(post)
                    pid = post.get("id", "")
                    if pid and st.button(
                        "Dismiss", key=f"dismiss_prog_{pid}", use_container_width=True
                    ):
                        db.table("posts").update({"status": "dismissed"}).eq("id", pid).execute()
                        st.cache_data.clear()
                        st.rerun()

# ── Scheduled ─────────────────────────────────────────────────────────────────

with tab_scheduled:
    if not scheduled:
        st.info("Nothing scheduled yet.")
    else:
        sched_sorted = sorted(scheduled, key=lambda p: p.get("scheduled_time") or "")

        # Content-type filter pills
        _type_filter = st.radio(
            "Filter by type",
            options=["All", "Carousel", "Reel", "Infographic", "Standard"],
            index=0,
            horizontal=True,
            label_visibility="collapsed",
            key="sched_type_filter",
        )
        if _type_filter == "Carousel":
            sched_sorted = [p for p in sched_sorted if p.get("post_type") == "carousel"]
        elif _type_filter == "Reel":
            sched_sorted = [p for p in sched_sorted if p.get("post_type") == "reel"]
        elif _type_filter == "Infographic":
            sched_sorted = [
                p
                for p in sched_sorted
                if p.get("post_type")
                in (
                    "infographic_reel",
                    "infographic_static",
                    "infographic_wheel",
                    "infographic_dark",
                    "infographic_light",
                    "infographic_rich_dark",
                    "infographic_rich_light",
                )
            ]
        elif _type_filter == "Standard":
            sched_sorted = [
                p
                for p in sched_sorted
                if p.get("post_type")
                not in (
                    "carousel",
                    "reel",
                    "infographic_reel",
                    "infographic_static",
                    "infographic_wheel",
                    "infographic_dark",
                    "infographic_light",
                    "infographic_rich_dark",
                    "infographic_rich_light",
                )
            ]

        if not sched_sorted:
            st.info(f"No {_type_filter.lower()} posts scheduled.")
        else:
            cols = st.columns(3)
            for i, p in enumerate(sched_sorted):
                with cols[i % 3]:
                    with st.container(border=True):
                        _post_card(p, _sched_str(p), "scheduled")
                        pid = p.get("id", "")
                        with st.expander("✏️ Edit caption"):
                            cur_caption = p.get("caption") or ""
                            new_cap = st.text_area(
                                "Caption",
                                value=cur_caption,
                                key=f"edit_cap_{pid}",
                                height=130,
                                label_visibility="collapsed",
                            )
                            if st.button("Save caption", key=f"save_cap_{pid}", type="primary"):
                                db.table("posts").update({"caption": new_cap}).eq(
                                    "id", pid
                                ).execute()
                                st.cache_data.clear()
                                st.rerun()
                        if pid and st.button(
                            "Publish now",
                            key=f"pub_{pid}",
                            use_container_width=True,
                            type="primary",
                        ):
                            db.table("posts").update(
                                {"scheduled_time": datetime.now(UTC).isoformat()}
                            ).eq("id", pid).execute()
                            try:
                                _queue_command("publish", cooldown_key=f"pub_{pid}")
                            except RuntimeError:
                                pass
                        if pid and st.button(
                            "Dismiss", key=f"dismiss_sched_{pid}", use_container_width=True
                        ):
                            db.table("posts").update({"status": "dismissed"}).eq(
                                "id", pid
                            ).execute()
                            st.cache_data.clear()
                            st.rerun()

# ── Generated ─────────────────────────────────────────────────────────────────

# Track posts actioned this session so they disappear immediately without
# needing a second st.rerun() (which resets the active tab to 0).
if "_gen_hidden" not in st.session_state:
    st.session_state["_gen_hidden"] = set()

with tab_generated:
    # Filter out posts the user has already actioned this session.
    visible_generated = [p for p in generated if p.get("id") not in st.session_state["_gen_hidden"]]
    if not visible_generated:
        st.info(
            "No manually generated posts yet. "
            "Use Generate Infographic or Generate Posts to create content — "
            "it will appear here for you to review before posting."
        )
    else:
        st.markdown(
            f"<div style='font-size:13px;color:{SLATE};margin-bottom:12px'>"
            f"{len(visible_generated)} post(s) ready — choose to post immediately or "
            f"add to the schedule queue.</div>",
            unsafe_allow_html=True,
        )
        gen_sorted = sorted(
            visible_generated, key=lambda p: p.get("created_at") or "", reverse=True
        )
        cols = st.columns(3)
        for i, p in enumerate(gen_sorted):
            with cols[i % 3]:
                with st.container(border=True):
                    _post_card(p, "Ready to post", "manual_ready")
                    pid = p.get("id", "")
                    _is_tg_post = (p.get("meta") or {}).get("delivery") == "telegram"
                    if pid:
                        if _is_tg_post:
                            # Instagram post delivered to Telegram — user posts natively
                            st.markdown(
                                "<div style='background:#E8F5E9;border:1px solid #A5D6A7;"
                                "border-radius:10px;padding:8px 12px;font-size:12px;"
                                "font-weight:600;color:#2E7D32;margin-bottom:6px'>"
                                "📱 Sent to your Telegram — save image &amp; post in Instagram app"
                                "</div>",
                                unsafe_allow_html=True,
                            )
                            btn_tg, btn_done = st.columns(2)
                            with btn_tg:
                                if st.button(
                                    "🔁 Resend",
                                    key=f"gen_resend_{pid}",
                                    use_container_width=True,
                                    help="Send the image and caption to Telegram again.",
                                ):
                                    db.table("posts").update(
                                        {
                                            "status": "scheduled",
                                            "scheduled_time": datetime.now(UTC).isoformat(),
                                        }
                                    ).eq("id", pid).execute()
                                    try:
                                        _queue_command("publish", cooldown_key=f"pub_{pid}")
                                    except RuntimeError:
                                        pass
                                    st.cache_data.clear()
                            with btn_done:
                                if st.button(
                                    "✅ Mark Posted",
                                    key=f"gen_markposted_{pid}",
                                    use_container_width=True,
                                    type="primary",
                                    help="Confirm you've posted this in Instagram — marks it as published.",
                                ):
                                    db.table("posts").update(
                                        {
                                            "status": "published",
                                            "published_time": datetime.now(UTC).isoformat(),
                                            "platform_post_id": "manual",
                                        }
                                    ).eq("id", pid).execute()
                                    st.session_state["_gen_hidden"].add(pid)
                                    st.cache_data.clear()
                        else:
                            btn1, btn2 = st.columns(2)
                            with btn1:
                                if st.button(
                                    "📤 Post Now",
                                    key=f"gen_postnow_{pid}",
                                    use_container_width=True,
                                    type="primary",
                                    help="Publish to the platform within ~2 minutes.",
                                ):
                                    db.table("posts").update(
                                        {
                                            "status": "scheduled",
                                            "scheduled_time": datetime.now(UTC).isoformat(),
                                        }
                                    ).eq("id", pid).execute()
                                    try:
                                        _queue_command("publish", cooldown_key=f"pub_{pid}")
                                    except RuntimeError:
                                        pass
                                    st.session_state["_gen_hidden"].add(pid)
                                    st.cache_data.clear()
                            with btn2:
                                if st.button(
                                    "📅 Schedule",
                                    key=f"gen_sched_{pid}",
                                    use_container_width=True,
                                    help="Let the auto-scheduler find the next optimal slot.",
                                ):
                                    try:
                                        _queue_command(
                                            f"schedule_post|{pid}",
                                            cooldown_key=f"schedpost_{pid}",
                                        )
                                        st.session_state["_gen_hidden"].add(pid)
                                        st.cache_data.clear()
                                    except RuntimeError:
                                        st.warning("Already scheduling this post.")
                        if st.button(
                            "Dismiss",
                            key=f"gen_dismiss_{pid}",
                            use_container_width=True,
                        ):
                            db.table("posts").update({"status": "dismissed"}).eq(
                                "id", pid
                            ).execute()
                            st.session_state["_gen_hidden"].add(pid)
                            st.cache_data.clear()

# ── Calendar ──────────────────────────────────────────────────────────────────

with tab_calendar:
    all_active = [p for p in posts if p.get("status") not in ("failed", "draft", "dismissed")]
    date_posts: dict[date, list[dict]] = defaultdict(list)
    for p in all_active:
        raw = p.get("scheduled_time") or p.get("published_time") or ""
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                date_posts[dt.date()].append(p)
            except Exception:
                pass

    today = datetime.now(UTC).date()
    if "cal_year" not in st.session_state:
        st.session_state.cal_year = today.year
    if "cal_month" not in st.session_state:
        st.session_state.cal_month = today.month

    col_p, col_t, col_n = st.columns([1, 5, 1])
    with col_p:
        if st.button("← Prev", use_container_width=True):
            if st.session_state.cal_month == 1:
                st.session_state.cal_month = 12
                st.session_state.cal_year -= 1
            else:
                st.session_state.cal_month -= 1
            st.rerun()
    with col_t:
        mname = datetime(st.session_state.cal_year, st.session_state.cal_month, 1).strftime("%B %Y")
        st.markdown(
            f'<div style=\'font-family:"Figtree",sans-serif;font-size:26px;'
            f"font-weight:700;letter-spacing:-0.02em;color:{CHARCOAL};text-align:center;"
            f"padding:4px 0'>{mname}</div>",
            unsafe_allow_html=True,
        )
    with col_n:
        if st.button("Next →", use_container_width=True):
            if st.session_state.cal_month == 12:
                st.session_state.cal_month = 1
                st.session_state.cal_year += 1
            else:
                st.session_state.cal_month += 1
            st.rerun()

    year = st.session_state.cal_year
    month = st.session_state.cal_month
    cal = calendar.monthcalendar(year, month)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    header_html = "".join(
        f'<div style="text-align:center;font-size:10px;font-weight:600;letter-spacing:0.1em;'
        f'text-transform:uppercase;color:{SILVER};padding:8px 0">{d}</div>'
        for d in days
    )
    cells_html = ""
    for week in cal:
        for day_num in week:
            if day_num == 0:
                cells_html += '<div style="min-height:80px"></div>'
                continue
            d = date(year, month, day_num)
            is_today = d == today
            day_posts = date_posts.get(d, [])
            border = f"2px solid {ACCENT}" if is_today else f"1px solid {SMOKE}"
            num_color = ACCENT if is_today else CHARCOAL
            pills = ""
            for p in day_posts:
                plat = (p.get("platform") or "").lower()
                c = PLATFORM_COLORS.get(plat, SLATE)
                abbr = PLATFORM_ABBR.get(plat, plat[:2].upper())
                t = html.escape(str(p.get("topic", "")))
                pills += (
                    f'<div style="background:{c};color:#fff;font-size:9px;font-weight:700;'
                    f"padding:2px 5px;border-radius:8px;white-space:nowrap;"
                    f'line-height:1.4;flex-shrink:0" title="{t}">{abbr}</div>'
                )
            cells_html += (
                f'<div style="background:{WHITE};border:{border};border-radius:12px;'
                f'min-height:90px;padding:8px;display:flex;flex-direction:column">'
                f'<div style="font-size:13px;font-weight:700;color:{num_color};margin-bottom:4px">'
                f"{day_num}</div>"
                f'<div style="display:flex;flex-wrap:wrap;gap:3px;overflow-y:auto;max-height:68px">'
                f"{pills}</div>"
                f"</div>"
            )

    cal_html = (
        "<!DOCTYPE html><html><head>"
        '<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;600;700&display=swap" rel="stylesheet">'
        "<style>*{margin:0;padding:0;box-sizing:border-box;"
        "font-family:'Figtree',sans-serif}"
        f"body{{background:{WHITE};padding:6px}}</style>"
        "</head><body>"
        f'<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:5px">'
        f"{header_html}{cells_html}</div></body></html>"
    )
    components.html(cal_html, height=len(cal) * 110 + 48, scrolling=False)

    legend = " &nbsp; ".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;'
        f'font-size:12px;color:{SLATE}">'
        f'<span style="background:{c};color:#fff;font-size:9px;font-weight:700;'
        f'padding:2px 5px;border-radius:8px">{PLATFORM_ABBR.get(p, p[:2].upper())}</span>'
        f"{p.title()}</span>"
        for p, c in PLATFORM_COLORS.items()
    )
    st.markdown(f"<div style='margin-top:10px'>{legend}</div>", unsafe_allow_html=True)

    st.divider()
    col_d, col_m, col_y = st.columns(3)
    with col_d:
        sel_day = st.number_input("Day", 1, 31, today.day, label_visibility="collapsed")
    with col_m:
        sel_mn = st.selectbox(
            "Month", list(calendar.month_name)[1:], index=month - 1, label_visibility="collapsed"
        )
        sel_m = list(calendar.month_name).index(sel_mn)
    with col_y:
        sel_y = st.number_input("Year", 2026, 2030, year, label_visibility="collapsed")
    try:
        sel_date = date(sel_y, sel_m, int(sel_day))
        day_items = date_posts.get(sel_date, [])
        if day_items:
            st.caption(f"{len(day_items)} post(s) on {sel_date.strftime('%A %d %B %Y')}")
            dcols = st.columns(min(len(day_items), 3))
            for i, p in enumerate(day_items):
                with dcols[i % 3]:
                    with st.container(border=True):
                        _post_card(p, _sched_str(p), p.get("status", "scheduled"))
        else:
            st.caption(f"No posts on {sel_date.strftime('%A %d %B %Y')}.")
    except ValueError:
        st.warning("Invalid date.")

    month_items = [
        p for d, ps in date_posts.items() for p in ps if d.year == year and d.month == month
    ]
    if month_items:
        pcounts = Counter(p.get("platform", "").lower() for p in month_items)
        st.markdown(
            f'<div style=\'font-family:"Figtree",sans-serif;'
            f"font-size:18px;font-weight:700;color:{CHARCOAL};margin-top:12px'>"
            f"{mname} — {len(month_items)} posts</div>",
            unsafe_allow_html=True,
        )
        scols = st.columns(len(pcounts))
        for i, (plat, cnt) in enumerate(sorted(pcounts.items(), key=lambda x: -x[1])):
            c = PLATFORM_COLORS.get(plat, SLATE)
            with scols[i]:
                st.markdown(
                    f'<div style="background:{c}12;border:1px solid {c}30;'
                    f'border-radius:14px;padding:12px;text-align:center;margin-top:6px">'
                    f'<div style="font-size:24px;font-weight:700;'
                    f"font-family:'Figtree',sans-serif;color:{c}\">{cnt}</div>"
                    f'<div style="font-size:10px;font-weight:600;letter-spacing:0.08em;'
                    f'text-transform:uppercase;color:{c};opacity:0.75;margin-top:3px">{plat}</div>'
                    f"</div>",
                    unsafe_allow_html=True,
                )

# ── Published ─────────────────────────────────────────────────────────────────

with tab_published:
    if not published:
        st.info("Nothing published yet — posts will appear here once live.")
    else:
        pub_sorted = sorted(
            published,
            key=lambda p: p.get("published_time") or p.get("scheduled_time") or "",
            reverse=True,
        )
        cols = st.columns(3)
        for i, post in enumerate(pub_sorted):
            with cols[i % 3]:
                with st.container(border=True):
                    _post_card(
                        post,
                        _sched_str(post),
                        "published",
                        analytics=analytics_by_post.get(post.get("id", "")),
                    )

# ── Analytics ─────────────────────────────────────────────────────────────────

_ANALYTICS_TABLE_SQL = """CREATE TABLE IF NOT EXISTS post_analytics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id uuid NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    platform text NOT NULL,
    platform_post_id text NOT NULL,
    snapshot_type text NOT NULL CHECK (snapshot_type IN ('24h','7d')),
    fetched_at timestamptz NOT NULL DEFAULT now(),
    reach integer, impressions integer, likes integer,
    comments integer, shares integer, saves integer, video_views integer,
    raw_data jsonb,
    UNIQUE(post_id, snapshot_type)
);
CREATE INDEX IF NOT EXISTS post_analytics_post_id_idx ON post_analytics(post_id);
CREATE INDEX IF NOT EXISTS post_analytics_fetched_at_idx ON post_analytics(fetched_at);

-- Let the worker write metrics (matches the other tables in this project,
-- which the worker reads & writes with the same key). Without this, inserts
-- fail with: 'new row violates row-level security policy'.
ALTER TABLE post_analytics DISABLE ROW LEVEL SECURITY;"""

# Shown on its own when a fetch completed but every insert was rejected by
# Supabase Row-Level Security (error code 42501) — the table exists but the
# worker's key isn't allowed to write to it.
_ANALYTICS_RLS_FIX_SQL = "ALTER TABLE post_analytics DISABLE ROW LEVEL SECURITY;"


def _render_analytics_diagnostics():
    """Inspect published posts and report why analytics may be empty.

    Runs entirely off data the dashboard already has (the posts list + env),
    so the user gets a concrete reason instead of a blank tab. The most
    common causes are: DRY_RUN left on (fake 'dry-run' ids), posts with no
    platform id, or analytics tokens not configured.
    """
    published = [p for p in posts if p.get("status") == "published"]
    total = len(published)
    real = [
        p for p in published if p.get("platform_post_id") and p.get("platform_post_id") != "dry-run"
    ]
    dry = [p for p in published if p.get("platform_post_id") == "dry-run"]
    missing = [p for p in published if not p.get("platform_post_id")]

    tokens = {
        "Instagram": bool(os.getenv("INSTAGRAM_ACCESS_TOKEN")),
        "Facebook": bool(
            os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN") or os.getenv("INSTAGRAM_ACCESS_TOKEN")
        ),
        "LinkedIn": bool(os.getenv("LINKEDIN_ACCESS_TOKEN")),
        "TikTok": bool(os.getenv("TIKTOK_ACCESS_TOKEN")),
        "YouTube": bool(os.getenv("YOUTUBE_REFRESH_TOKEN")),
    }

    st.markdown(f"**{total}** published posts in total:")
    st.markdown(f"- ✅ **{len(real)}** have a real platform post ID (these can return metrics)")
    if dry:
        st.markdown(
            f"- ⚠️ **{len(dry)}** are marked `dry-run` — these were **never actually posted "
            "live**, so no metrics exist for them. This happens when `DRY_RUN` is left on. "
            "Set `DRY_RUN=false` in your worker's environment variables to publish for real."
        )
    if missing:
        st.markdown(f"- ⚠️ **{len(missing)}** have no platform post ID stored")
    tok_str = "  ".join(f"{'✅' if v else '❌'} {k}" for k, v in tokens.items())
    st.markdown(f"**Analytics API tokens** (as seen by this dashboard): {tok_str}")
    st.caption(
        "Metrics can only be fetched for posts with a real platform ID *and* a configured "
        "API token for that platform. If your worker runs as a separate service, its tokens "
        "may differ from what's shown here."
    )


def _render_analytics_fetch_button():
    """Fetch button + status of the most recent analytics run, so the user can
    see whether the worker picked it up and what it returned."""
    if st.button("📊 Fetch latest metrics", type="primary"):
        try:
            _queue_command("analytics")
            load_analytics.clear()
        except RuntimeError as e:
            st.error(str(e))
    last = load_last_command_status(db, "analytics")
    if last:
        cmd_id = last.get("id", "")
        status = last.get("status", "?")
        when = (last.get("finished_at") or last.get("requested_at") or "")[:19].replace("T", " ")
        if status == "done":
            msg = last.get("error") or ""  # doubles as the result summary on success
            detail = f" — {msg}" if msg else ""
            st.caption(f"Last fetch: ✅ completed at {when} UTC{detail}")
            # Detect the most common blocker: the fetch worked but every write
            # was rejected by Supabase Row-Level Security. Surface the one-line
            # fix directly instead of leaving it buried in the status caption.
            if "row-level security" in msg.lower() or "42501" in msg:
                _sql_editor_url = _supabase_sql_editor_url()
                st.warning(
                    "Metrics were fetched successfully, but your database is "
                    "**blocking the worker from saving them** (row-level security "
                    "is on for `post_analytics`). One line fixes it:"
                )
                st.code(_ANALYTICS_RLS_FIX_SQL, language="sql")
                st.markdown(
                    f"**[Open your Supabase SQL Editor]({_sql_editor_url})**, paste the "
                    "line above, click **Run**, then come back and hit "
                    "**Fetch latest metrics** again."
                )
            # If the fetch succeeded but cached data is still empty, clear the
            # cache once per command so the next render picks up the new rows.
            elif not analytics_rows and not analytics_error and cmd_id:
                _refresh_key = f"_analytics_refreshed_{cmd_id}"
                if not st.session_state.get(_refresh_key):
                    st.session_state[_refresh_key] = True
                    load_analytics.clear()
                    st.rerun()
        elif status == "failed":
            st.caption(
                f"Last fetch: ❌ failed at {when} UTC — {last.get('error') or 'unknown error'}"
            )
        elif status in ("pending", "running"):
            st.caption(f"Last fetch: ⏳ {status} (queued {when} UTC) — refresh shortly")


with tab_analytics:
    if analytics_error:
        _sql_editor_url = _supabase_sql_editor_url()
        st.error("The **post_analytics** table doesn't exist in your database yet.")
        st.markdown(
            f"""
**To set it up (takes about 30 seconds):**

1. **[Open your Supabase SQL Editor]({_sql_editor_url})** — this link goes directly to it
2. Copy the SQL below
3. Paste it into the editor and click **Run**
4. Come back here and click **Fetch latest metrics**
"""
        )
        st.code(_ANALYTICS_TABLE_SQL, language="sql")
        with st.expander("Error detail"):
            st.caption(analytics_error)
        _render_analytics_fetch_button()
    elif not analytics_rows:
        st.info(
            "No analytics data yet — metrics are fetched automatically at 24h and 7d "
            "after each post is published, and a backfill pass picks up older posts on "
            "each fetch."
        )
        _render_analytics_diagnostics()
        _render_analytics_fetch_button()
    else:
        # --- Enrich analytics with post metadata ---
        posts_by_id_analytics = {p["id"]: p for p in posts if p.get("id")}

        def _an_reach(r: dict) -> int:
            return r.get("reach") or r.get("impressions") or 0

        enriched_analytics = []
        for r in analytics_rows:
            post_meta = posts_by_id_analytics.get(r["post_id"], {})
            enriched_analytics.append({**r, "_post": post_meta})

        # Use best snapshot per post for summary stats.
        best_analytics: dict[str, dict] = {}
        for r in enriched_analytics:
            pid = r["post_id"]
            existing = best_analytics.get(pid)
            if existing is None or r["snapshot_type"] == "7d":
                best_analytics[pid] = r
        best_list = list(best_analytics.values())

        # --- Summary metrics bar ---
        total_reach = sum(_an_reach(r) for r in best_list)
        avg_reach = int(total_reach / len(best_list)) if best_list else 0

        from collections import defaultdict as _defaultdict

        plat_reach_sums: dict = _defaultdict(list)
        pillar_reach_sums: dict = _defaultdict(list)
        for r in best_list:
            plat_reach_sums[r.get("platform", "Unknown")].append(_an_reach(r))
            pillar_reach_sums[r["_post"].get("pillar", "Unknown")].append(_an_reach(r))

        def _avg_list(lst: list) -> int:
            return int(sum(lst) / len(lst)) if lst else 0

        best_platform = max(
            plat_reach_sums, key=lambda p: _avg_list(plat_reach_sums[p]), default="—"
        )
        best_pillar = max(
            pillar_reach_sums, key=lambda p: _avg_list(pillar_reach_sums[p]), default="—"
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Reach", _compact_num(total_reach))
        m2.metric("Avg Reach / Post", _compact_num(avg_reach))
        m3.metric("Best Platform", best_platform.title())
        m4.metric("Best Pillar", best_pillar)

        st.markdown("---")

        # --- Top 10 posts by reach ---
        sorted_best = sorted(best_list, key=_an_reach, reverse=True)
        st.markdown(
            "<div style='font-size:16px;font-weight:700;color:#1D1D1F;padding:8px 0 4px'>Top 10 Posts by Reach</div>",
            unsafe_allow_html=True,
        )
        for rank, r in enumerate(sorted_best[:10], 1):
            post_meta = r["_post"]
            t = post_meta.get("title") or post_meta.get("topic") or "Untitled"
            plat = r.get("platform", "—")
            pillar = post_meta.get("pillar", "—")
            reach_val = _an_reach(r)
            likes_val = r.get("likes") or 0
            snap = r.get("snapshot_type", "")
            plat_color = PLATFORM_COLORS.get(plat.lower(), "#6E6E73")
            st.markdown(
                f"<div style='padding:6px 0;border-bottom:1px solid #F5F5F7'>"
                f"<span style='font-weight:700;color:#1D1D1F;font-size:13px'>{rank}. {html.escape(str(t))}</span>"
                f"&nbsp; <span style='background:{plat_color}18;color:{plat_color};border-radius:9999px;"
                f"padding:2px 8px;font-size:11px;font-weight:700'>{html.escape(str(plat))}</span>"
                f"&nbsp; <span style='font-size:11px;color:#6E6E73'>{html.escape(str(pillar))}</span>"
                f"&nbsp;&nbsp; <span style='font-size:12px;color:#1D1D1F'>👁 {_compact_num(reach_val)}"
                f"&nbsp; ❤ {_compact_num(likes_val)}</span>"
                f"&nbsp; <span style='font-size:10px;color:#A1A1A6'>[{snap}]</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='margin-bottom:16px'></div>", unsafe_allow_html=True)

        # --- Platform breakdown ---
        col_pb, col_pi = st.columns(2)
        with col_pb:
            st.markdown(
                "<div style='font-size:14px;font-weight:700;color:#1D1D1F;padding:4px 0'>Platform Avg Reach</div>",
                unsafe_allow_html=True,
            )
            if plat_reach_sums:
                import pandas as _pd

                plat_df = _pd.DataFrame(
                    [
                        {"Platform": p.title(), "Avg Reach": _avg_list(v)}
                        for p, v in sorted(plat_reach_sums.items(), key=lambda x: -_avg_list(x[1]))
                    ]
                ).set_index("Platform")
                st.bar_chart(plat_df)

        with col_pi:
            st.markdown(
                "<div style='font-size:14px;font-weight:700;color:#1D1D1F;padding:4px 0'>Pillar Avg Reach</div>",
                unsafe_allow_html=True,
            )
            if pillar_reach_sums:
                import pandas as _pd

                pillar_df = _pd.DataFrame(
                    [
                        {"Pillar": p, "Avg Reach": _avg_list(v)}
                        for p, v in sorted(
                            pillar_reach_sums.items(), key=lambda x: -_avg_list(x[1])
                        )
                    ]
                ).set_index("Pillar")
                st.bar_chart(pillar_df)

        # --- Bottom performers ---
        sorted_worst = sorted(best_list, key=_an_reach)
        bottom5 = sorted_worst[:5]
        with st.expander("Bottom Performers (avoid these topics)"):
            for r in bottom5:
                post_meta = r["_post"]
                t = post_meta.get("title") or post_meta.get("topic") or "Untitled"
                plat = r.get("platform", "—")
                reach_val = _an_reach(r)
                st.markdown(
                    f"- **{html.escape(str(t))}** ({html.escape(str(plat))}) — "
                    f"👁 {_compact_num(reach_val)} reach",
                    unsafe_allow_html=True,
                )

        st.markdown("---")

        # --- Fetch latest metrics button ---
        _render_analytics_fetch_button()


# ── Pipeline flowchart ────────────────────────────────────────────────────────

with tab_pipeline:
    st.markdown(
        '<div style=\'font-family:"Figtree",sans-serif;font-size:22px;'
        f"font-weight:700;letter-spacing:-0.02em;color:{CHARCOAL};margin-bottom:12px'>"
        "How the pipeline works</div>",
        unsafe_allow_html=True,
    )

    FLOW_HTML = r"""<!DOCTYPE html><html><head>
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700;800&family=Playfair+Display:ital@1&display=swap" rel="stylesheet">
<style>
* { margin:0; padding:0; box-sizing:border-box; font-family:'Figtree',sans-serif; }
body { background:#F5F5F7; color:#1D1D1F; padding:20px; font-size:13px; }
.row { display:flex; align-items:flex-start; justify-content:center; gap:12px; margin-bottom:6px; }
.arr { text-align:center; color:#A1A1A6; font-size:22px; margin:2px 0; }
.node {
  background:#FFFFFF; border:1px solid #E8E8ED; border-radius:16px;
  padding:13px 17px; text-align:center; min-width:140px; max-width:180px;
  box-shadow:0 1px 3px rgba(0,0,0,0.04);
}
.node .label {
  font-family:'Figtree',sans-serif; font-size:15px; font-weight:700;
  letter-spacing:-0.01em; color:#1D1D1F; line-height:1.2;
}
.node .sub { font-size:11px; color:#6E6E73; margin-top:4px; line-height:1.45; }
.node .badge {
  display:inline-block; border-radius:980px; padding:2px 10px;
  font-size:10px; font-weight:600; letter-spacing:0.06em; text-transform:uppercase;
  margin-top:6px;
}
.node.trigger { border-color:#B25E0940; background:#B25E090A; }
.node.trigger .label { color:#B25E09; }
.node.gate { border-color:#1D7A3440; background:#1D7A340A; }
.node.gate .label { color:#1D7A34; }
.node.media { border-color:#E8E8ED; background:#FFFFFF; }
.node.media .label { color:#1D1D1F; }
.node.publish { border-color:#0066CC40; background:#E8F0FA; }
.node.publish .label { color:#0066CC; }
.node.live { border-color:#1D7A3460; background:#1D7A340F; }
.node.live .label { color:#1D7A34; }
.tag { font-size:10px; color:#A1A1A6; letter-spacing:0.08em; text-transform:uppercase; margin-bottom:4px; }
.mini { font-size:11px; color:#A1A1A6; letter-spacing:0.08em; text-transform:uppercase; }
</style>
</head><body>

<!-- Row 1: Two trigger nodes -->
<div class="row">
  <div class="node trigger">
    <div class="tag">Daily 05:30</div>
    <div class="label">Research Agent</div>
    <div class="sub">Searches the web for trending topics across your 5 niches. Scores each for brand fit.</div>
  </div>
  <div style="min-width:12px"></div>
  <div class="node trigger">
    <div class="tag">Monday 07:00</div>
    <div class="label">Weekly Strategy</div>
    <div class="sub">Studies competitor accounts &amp; viral patterns. Generates 7 shaped ideas.</div>
  </div>
</div>

<!-- Arrows down -->
<div class="row"><div class="arr">↓</div><div style="min-width:60px"></div><div class="arr">↓</div></div>

<!-- Row 2: Approval gate -->
<div class="row">
  <div class="node gate" style="min-width:340px;max-width:400px">
    <div class="label">Approval Queue</div>
    <div class="sub">Topics land here. <b style="color:#1D7A34">You approve or reject each one.</b><br>
    Nothing moves forward without your sign-off. Use the Topics tab above.</div>
  </div>
</div>

<div class="row"><div class="arr">↓</div></div>
<div class="row"><div class="mini">every 15 min</div></div>
<div class="row"><div class="arr">↓</div></div>

<!-- Row 3: Content agent -->
<div class="row">
  <div class="node" style="min-width:300px">
    <div class="label">Content Agent</div>
    <div class="sub">Writes the caption, hashtags, and title for each approved topic. Uses Claude Sonnet 4.6.</div>
  </div>
</div>

<div class="row"><div class="arr">↓</div></div>

<!-- Cross-post note -->
<div class="row">
  <div class="node gate" style="min-width:340px;max-width:420px">
    <div class="label" style="font-size:13px">Auto cross-post to Facebook</div>
    <div class="sub">Every Instagram <b>and</b> LinkedIn topic also spawns a matching Facebook carousel — same caption, so Facebook always gets coverage.</div>
  </div>
</div>

<div class="row"><div class="arr">↓</div></div>

<!-- Row 4: Platform fork -->
<div class="row" style="align-items:stretch">
  <div class="node media" style="max-width:210px">
    <div class="tag">Instagram · Facebook</div>
    <div class="label">Carousel Agent</div>
    <div class="sub">Claude Sonnet plans the copy; 4 text slides (cover, 2 value cards, CTA) rendered with Pillow on brand scene backgrounds. No image model — slides can never fail to generate.</div>
  </div>
  <div style="display:flex;align-items:center;padding:0 8px">
    <div style="width:1px;height:60px;background:#E8E8ED"></div>
  </div>
  <div class="node media" style="max-width:210px">
    <div class="tag">Twitter · LinkedIn · YouTube · TikTok</div>
    <div class="label">Thumbnail Agent</div>
    <div class="sub">Single editorial photo from Imagen 4 Fast. Brand logo composited in quietest corner. (YouTube/TikTok also get a HeyGen video.)</div>
  </div>
</div>

<div class="row"><div class="arr">↓</div></div>

<!-- Row 5: Scheduler -->
<div class="row">
  <div class="node publish" style="min-width:300px">
    <div class="label">Scheduler Agent</div>
    <div class="sub">Finds the best time slot for each platform based on peak-engagement windows. Status → <b style="color:#0066CC">scheduled</b>.</div>
  </div>
</div>

<div class="row"><div class="arr">↓</div></div>
<div class="row"><div class="mini">every 5 min</div></div>
<div class="row"><div class="arr">↓</div></div>

<!-- Row 6: Publisher -->
<div class="row">
  <div class="node publish" style="min-width:300px">
    <div class="label">Publisher Agent</div>
    <div class="sub">Checks for posts whose scheduled time has passed and sends them to each platform's API.</div>
  </div>
</div>

<div class="row"><div class="arr">↓</div></div>

<!-- Row 7: Live -->
<div class="row">
  <div class="node live" style="min-width:300px">
    <div class="label">Live on Platform</div>
    <div class="sub">Status → <b style="color:#1D7A34">published</b>. Appears in the Published tab.</div>
  </div>
</div>

<div style="margin-top:28px;padding-top:16px;border-top:1px solid #E8E8ED">
  <div style="font-family:'Figtree',sans-serif;font-size:11px;font-weight:600;
              color:#A1A1A6;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:12px">
    Background jobs</div>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <div class="node" style="min-width:0;max-width:none;flex:1;text-align:left;padding:12px 16px">
      <div class="label" style="font-size:13px">QC Retry  <span style="color:#A1A1A6;font-weight:400;font-size:11px">every 4 hrs</span></div>
      <div class="sub">Re-generates thumbnails that failed the image quality check.</div>
    </div>
    <div class="node" style="min-width:0;max-width:none;flex:1;text-align:left;padding:12px 16px">
      <div class="label" style="font-size:13px">Image Refresh  <span style="color:#A1A1A6;font-weight:400;font-size:11px">daily 02:00</span></div>
      <div class="sub">Rebuilds carousel slides (and single thumbnails) for any scheduled or failed post still missing its images.</div>
    </div>
    <div class="node" style="min-width:0;max-width:none;flex:1;text-align:left;padding:12px 16px">
      <div class="label" style="font-size:13px">Analytics  <span style="color:#A1A1A6;font-weight:400;font-size:11px">every 2 hrs</span></div>
      <div class="sub">Pulls reach, impressions, likes &amp; comments for each post 24 h and 7 d after publish.</div>
    </div>
    <div class="node" style="min-width:0;max-width:none;flex:1;text-align:left;padding:12px 16px">
      <div class="label" style="font-size:13px">Cleanup  <span style="color:#A1A1A6;font-weight:400;font-size:11px">Sunday 03:00</span></div>
      <div class="sub">Prunes pipeline command rows older than 7 days.</div>
    </div>
  </div>
</div>

</body></html>"""

    components.html(FLOW_HTML, height=1140, scrolling=True)

# ── Failed alert ──────────────────────────────────────────────────────────────

if failed:
    st.divider()
    with st.expander(f"⚠  {len(failed)} failed post(s) — click to review"):
        col_retry_all, col_del_all, _ = st.columns([1, 1, 4])
        with col_retry_all:
            if st.button("Retry all", key="retry_all_failed", type="primary"):
                ids = [p["id"] for p in failed if p.get("id")]
                if ids:
                    db.table("posts").update({"status": "scheduled", "error": None}).in_(
                        "id", ids
                    ).execute()
                    st.rerun()
        with col_del_all:
            if st.button("Dismiss all", key="delete_all_failed"):
                ids = [p["id"] for p in failed if p.get("id")]
                if ids:
                    db.table("posts").update({"status": "dismissed"}).in_("id", ids).execute()
                    st.rerun()

        for post in failed:
            title = post.get("title") or post.get("topic", "Untitled")
            detail = post.get("error") or "No detail"
            post_id = post.get("id", "")
            col_err, col_retry, col_del = st.columns([5, 1, 1])
            with col_err:
                st.markdown(
                    f"<div style='background:{C_FAILED}12;border:1px solid {C_FAILED}30;"
                    f"border-radius:12px;padding:10px 14px;margin-bottom:4px'>"
                    f"<div style='font-weight:700;color:{CHARCOAL};font-size:13px'>"
                    f"{html.escape(str(title))} "
                    f"<span style='color:{SLATE};font-weight:400'>"
                    f"({html.escape(str(post.get('platform', '')))})</span>"
                    f"</div>"
                    f"<div style='font-size:12px;color:{C_FAILED};margin-top:4px'>"
                    f"{html.escape(str(detail))}</div></div>",
                    unsafe_allow_html=True,
                )
            with col_retry:
                if post_id and st.button("Retry", key=f"retry_{post_id}"):
                    db.table("posts").update({"status": "scheduled", "error": None}).eq(
                        "id", post_id
                    ).execute()
                    st.rerun()
            with col_del:
                if post_id and st.button("Dismiss", key=f"delete_{post_id}"):
                    db.table("posts").update({"status": "dismissed"}).eq("id", post_id).execute()
                    st.rerun()
