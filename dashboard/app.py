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

    /* ── Sidebar open/close toggle — keep visible across all Streamlit versions ──
         Test-ids have churned between releases, so target every known variant:
           • stSidebarCollapseButton  — collapse chevron inside the OPEN sidebar
           • stExpandSidebarButton    — expand button shown when CLOSED (1.4x)
           • stSidebarCollapsedControl / collapsedControl — older closed-state btn
         All forced visible because our toolbar display:none rule can bleed in,
         and the header is given height + z-index so the button can't be clipped. */
    [data-testid="stHeader"] { min-height: 2.875rem !important; z-index: 999990 !important; }
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stSidebarCollapseButton"] button,
    [data-testid="stExpandSidebarButton"],
    [data-testid="stExpandSidebarButton"] button,
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"] {
      display: flex !important; visibility: visible !important; opacity: 1 !important;
    }
    [data-testid="stExpandSidebarButton"],
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"] {
      z-index: 1000000 !important; top: 0.55rem !important; left: 0.55rem !important;
    }
    [data-testid="stSidebarCollapseButton"] button,
    [data-testid="stExpandSidebarButton"] button,
    [data-testid="stSidebarCollapsedControl"] button,
    [data-testid="collapsedControl"] button {
      background: var(--white) !important; border: 1px solid var(--smoke) !important;
      border-radius: 980px !important; color: var(--charcoal) !important;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08) !important;
    }
    [data-testid="stSidebarCollapseButton"] button:hover,
    [data-testid="stExpandSidebarButton"] button:hover,
    [data-testid="stSidebarCollapsedControl"] button:hover,
    [data-testid="collapsedControl"] button:hover {
      border-color: var(--charcoal) !important; background: var(--off-white) !important;
    }
    [data-testid="stSidebarCollapseButton"] svg,
    [data-testid="stExpandSidebarButton"] svg,
    [data-testid="stSidebarCollapsedControl"] svg,
    [data-testid="collapsedControl"] svg {
      color: var(--charcoal) !important; fill: var(--charcoal) !important;
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
       The wrapper div has no position:relative so stElementToolbar's
       position:absolute resolves against a high ancestor (stMain), putting
       it near the tab bar.  Make stFullScreenFrame the containing block and
       pin the toolbar to its top-right corner. */
    [data-testid="stFullScreenFrame"] {
      position: relative !important;
      overflow: visible !important;
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

    /* Tabs: remember the selected tab, re-select it once per rerun. */
    const list = doc.querySelector('.stTabs [data-baseweb="tab-list"]');
    if (list) {
      const tabs = list.querySelectorAll('[data-baseweb="tab"]');
      if (tabs.length) {
        if (!list.dataset.btlTabWired) {
          list.dataset.btlTabWired = '1';
          tabs.forEach((t, i) =>
            t.addEventListener('click', () => ss.setItem(TK, String(i)))
          );
        }
        if (!tabDone) {
          tabDone = true;
          const want = ss.getItem(TK);
          const idx = want !== null ? parseInt(want, 10) : NaN;
          const cur = list.querySelector('[aria-selected="true"]');
          const ci = Array.prototype.indexOf.call(tabs, cur);
          if (!isNaN(idx) && idx >= 0 && idx < tabs.length && idx !== ci) {
            tabs[idx].click();
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


def by_status(items, *statuses):
    return [i for i in items if i.get("status") in statuses]


pending = by_status(topics, "pending_approval")
approved_t = by_status(topics, "approved")
in_progress = by_status(posts, "content_ready", "media_ready")
scheduled = by_status(posts, "scheduled")
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

    if st.button("↺  Refresh data now", use_container_width=True, key=f"{scope}_refresh"):
        st.cache_data.clear()
        st.rerun()


with st.sidebar:
    st.markdown(
        f"""
<div style="padding:12px 0 22px">
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


def _post_card(post: dict, time_str: str = "", time_label: str = "") -> None:
    is_carousel = post.get("post_type") == "carousel"
    slides = post.get("slides") or []
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
    if url:
        st.image(url if url.endswith(".png") else url + ".png", use_container_width=True)
    else:
        st.markdown(
            f"<div style='background:{OFF_WHITE};border:1px solid {SMOKE};border-radius:12px;"
            "height:88px;display:flex;align-items:center;justify-content:center;"
            f"color:{SILVER};font-size:12px'>No image yet</div>",
            unsafe_allow_html=True,
        )

    pills = _pill(platform, plat_color) + " "
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

    st.markdown(
        f"""<div style='padding:8px 2px 4px'>
          <div style='margin-bottom:6px'>{pills}</div>
          <div style='font-family:"Figtree",sans-serif;font-size:17px;font-weight:700;
                      letter-spacing:-0.01em;color:{CHARCOAL};line-height:1.25;margin-bottom:3px'>
            {e_title}</div>
          <div style='font-size:11px;color:{SLATE}'>{e_pillar}</div>
        </div>""",
        unsafe_allow_html=True,
    )

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
    tab_calendar,
    tab_published,
    tab_pipeline,
) = st.tabs(
    [
        f"Topics  {len(pending)}",
        f"In Progress  {len(in_progress)}",
        f"Scheduled  {len(scheduled)}",
        "Calendar",
        f"Published  {len(published)}",
        "Pipeline",
    ]
)

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
        cols = st.columns(3)
        for i, p in enumerate(sched_sorted):
            with cols[i % 3]:
                with st.container(border=True):
                    _post_card(p, _sched_str(p), "scheduled")
                    pid = p.get("id", "")
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
                        db.table("posts").update({"status": "dismissed"}).eq("id", pid).execute()
                        st.cache_data.clear()
                        st.rerun()

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
            dots = ""
            for p in day_posts[:8]:
                plat = (p.get("platform") or "").lower()
                c = PLATFORM_COLORS.get(plat, SLATE)
                t = html.escape(str(p.get("topic", "")))
                dots += (
                    f'<div style="width:10px;height:10px;border-radius:50%;'
                    f'background:{c};flex-shrink:0" title="{t}"></div>'
                )
            count_html = (
                f'<div style="font-size:10px;font-weight:700;color:{ACCENT};margin-top:4px">'
                f"{len(day_posts)}</div>"
                if day_posts
                else ""
            )
            cells_html += (
                f'<div style="background:{WHITE};border:{border};border-radius:12px;'
                f'min-height:80px;padding:8px">'
                f'<div style="font-size:13px;font-weight:700;color:{num_color};margin-bottom:4px">'
                f"{day_num}</div>"
                f'<div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:2px">{dots}</div>'
                f"{count_html}</div>"
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
    components.html(cal_html, height=len(cal) * 93 + 48, scrolling=False)

    legend = " &nbsp; ".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;'
        f'font-size:12px;color:{SLATE}">'
        f'<span style="width:10px;height:10px;border-radius:50%;background:{c};'
        f'display:inline-block"></span>{p.title()}</span>'
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
                    _post_card(post, _sched_str(post), "published")

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
    <div class="sub">Writes the caption, hashtags, and title for each approved topic. Uses Claude Sonnet.</div>
  </div>
</div>

<div class="row"><div class="arr">↓</div></div>

<!-- Row 4: Platform fork -->
<div class="row" style="align-items:stretch">
  <div class="node media" style="max-width:200px">
    <div class="tag">Instagram · Facebook</div>
    <div class="label">Carousel Agent</div>
    <div class="sub">Plans 4–6 slides with Claude. Cover photo from Imagen. Numbered text cards for content. CTA card at the end.</div>
  </div>
  <div style="display:flex;align-items:center;padding:0 8px">
    <div style="width:1px;height:60px;background:#E8E8ED"></div>
  </div>
  <div class="node media" style="max-width:200px">
    <div class="tag">Twitter · LinkedIn</div>
    <div class="label">Thumbnail Agent</div>
    <div class="sub">Single editorial photo from Imagen. Brand logo composited in quietest corner.</div>
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
      <div class="sub">Regenerates any post that is missing a thumbnail image.</div>
    </div>
    <div class="node" style="min-width:0;max-width:none;flex:1;text-align:left;padding:12px 16px">
      <div class="label" style="font-size:13px">Cleanup  <span style="color:#A1A1A6;font-weight:400;font-size:11px">Sunday 03:00</span></div>
      <div class="sub">Prunes pipeline command rows older than 7 days.</div>
    </div>
  </div>
</div>

</body></html>"""

    components.html(FLOW_HTML, height=980, scrolling=True)

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
