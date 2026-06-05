"""Brite Tech Lifestyle — Automation Dashboard."""

from __future__ import annotations

import calendar
import hmac
import html
import logging
import os
import time
from collections import defaultdict
from datetime import UTC, date, datetime

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Brite Tech Lifestyle — Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS injection via components.html (0-height iframe — always reliable) ────

components.html(
    """
<script>
const css = `
  @import url('https://fonts.googleapis.com/css2?family=Figtree:wght@300;400;500;600;700;800&display=swap');
  html, body, [class*="css"], button, input, textarea { font-family: 'Figtree', sans-serif !important; }
  .stApp { background: #F5F5F7 !important; }
  #MainMenu, footer { visibility: hidden; }
  header[data-testid="stHeader"] { background: #000 !important; }
  .block-container { padding-top: 3.5rem !important; max-width: 1400px !important; }
  .stTabs [data-baseweb="tab-list"] { background: #fff; border-radius: 12px; padding: 4px; border: 1px solid #E8E8ED; gap: 2px; margin-bottom: 8px; }
  .stTabs [data-baseweb="tab"] { border-radius: 8px; font-family: 'Figtree', sans-serif !important; font-weight: 600 !important; font-size: 13px !important; color: #6E6E73; padding: 8px 18px; background: transparent; }
  .stTabs [aria-selected="true"] { background: #0066CC !important; color: #fff !important; }
  .stTabs [data-baseweb="tab-border"] { display: none !important; }
  .stButton > button { font-family: 'Figtree', sans-serif !important; font-weight: 600 !important; border-radius: 9999px !important; }
  .stButton > button[kind="primary"] { background: #0066CC !important; border-color: #0066CC !important; color: #fff !important; }
  [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 16px !important; border-color: #E8E8ED !important; background: #fff; }
`;
const style = document.createElement('style');
style.textContent = css;
window.parent.document.head.appendChild(style);

// Apply expander styles directly as inline styles (bypasses Streamlit's emotion CSS)
function styleExpanders() {
  const doc = window.parent.document;
  doc.querySelectorAll('[data-testid="stExpander"] details').forEach(function(details) {
    details.style.setProperty('border', '1px solid #E8E8ED', 'important');
    details.style.setProperty('border-radius', '12px', 'important');
    details.style.setProperty('overflow', 'hidden', 'important');
    details.style.setProperty('background', '#ffffff', 'important');
    details.style.setProperty('margin-bottom', '8px', 'important');
  });
  doc.querySelectorAll('[data-testid="stExpander"] summary').forEach(function(summary) {
    summary.style.setProperty('padding', '10px 14px', 'important');
    summary.style.setProperty('background', '#F5F5F7', 'important');
    summary.style.setProperty('background-color', '#F5F5F7', 'important');
    summary.style.setProperty('font-size', '13px', 'important');
    summary.style.setProperty('font-weight', '600', 'important');
    summary.style.setProperty('color', '#1D1D1F', 'important');
    summary.style.setProperty('cursor', 'pointer', 'important');
  });
}

// Run on load and on every DOM change (Streamlit re-renders frequently)
styleExpanders();
const observer = new MutationObserver(styleExpanders);
observer.observe(window.parent.document.body, { childList: true, subtree: true });

// Align "TECH LIFESTYLE" to exactly match the width of "Brite"
function alignBriteSub() {
  const brite = window.parent.document.querySelector('.btl-brite');
  const sub   = window.parent.document.querySelector('.btl-sub');
  if (!brite || !sub) return;
  sub.style.letterSpacing = '0px';
  sub.style.marginRight   = '0px';
  const briteW = brite.getBoundingClientRect().width;
  sub.style.display = 'inline';
  const subW = sub.getBoundingClientRect().width;
  sub.style.display = 'block';
  const chars = sub.textContent.trim().length;
  if (!chars || !briteW || !subW) return;
  const ls = (briteW - subW) / chars;
  sub.style.letterSpacing = ls + 'px';
  sub.style.marginRight   = (-ls) + 'px';
}
document.fonts.ready.then(function() {
  alignBriteSub();
  setTimeout(alignBriteSub, 800);
});
window.parent.addEventListener('resize', alignBriteSub);
</script>
""",
    height=0,
)

# ── Authentication ────────────────────────────────────────────────────────────


_AUTH_MAX_ATTEMPTS = 5
_AUTH_LOCKOUT_SECS = 900  # 15 minutes


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

    # Rate limiting: track failed attempts within the lockout window.
    now = datetime.now(UTC).timestamp()
    attempts: list[float] = [
        t for t in st.session_state.get("_auth_attempts", []) if now - t < _AUTH_LOCKOUT_SECS
    ]
    if len(attempts) >= _AUTH_MAX_ATTEMPTS:
        st.error("Too many failed attempts. Please wait 15 minutes and try again.")
        return False

    st.markdown("<br>" * 3, unsafe_allow_html=True)
    col = st.columns([1, 1, 1])[1]
    with col:
        st.markdown(
            """
        <div style="text-align:center;margin-bottom:24px">
          <div style="font-size:52px;font-weight:800;letter-spacing:-0.045em;color:#1D1D1F;line-height:1">Brite</div>
          <div style="font-size:10px;font-weight:300;letter-spacing:0.28em;color:#A1A1A6;text-transform:uppercase;margin-top:4px">Tech Lifestyle</div>
          <div style="font-size:14px;color:#6E6E73;margin-top:16px;font-weight:400">Automation Dashboard</div>
        </div>
        """,
            unsafe_allow_html=True,
        )
        pwd = st.text_input(
            "Password", type="password", placeholder="Enter password", label_visibility="collapsed"
        )
        if st.button("Sign In", use_container_width=True, type="primary"):
            # Constant-time comparison to prevent timing attacks.
            if hmac.compare_digest(pwd, expected):
                st.session_state["authenticated"] = True
                st.session_state["_auth_attempts"] = []
                st.rerun()
            else:
                attempts.append(now)
                st.session_state["_auth_attempts"] = attempts
                time.sleep(1)  # slow brute-force without a hard lockout
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


@st.cache_data(ttl=300)
def load_topics():
    return (
        db.table("topics").select("*").order("relevance_score", desc=True).limit(200).execute().data
        or []
    )


@st.cache_data(ttl=300)
def load_posts():
    return (
        db.table("posts").select("*").order("scheduled_time", desc=False).limit(500).execute().data
        or []
    )


topics = load_topics()
posts = load_posts()


def by_status(items, status):
    return [i for i in items if i.get("status") == status]


pending = by_status(topics, "pending_approval")
approved_t = by_status(topics, "approved")
content_ready = by_status(posts, "content_ready")
media_ready = by_status(posts, "media_ready")
scheduled = by_status(posts, "scheduled")
published = by_status(posts, "published")
failed = by_status(posts, "failed")

# ── Header ────────────────────────────────────────────────────────────────────

now_utc = datetime.now(UTC)

# Black branded nav bar
# Full-width branded header bar
st.markdown(
    f"""
<div style="background:#000;border-radius:16px;padding:18px 32px;margin-bottom:16px;
            display:flex;align-items:center;justify-content:space-between;
            box-sizing:border-box;width:100%">
  <div style="display:flex;align-items:center;gap:24px">
    <div style="line-height:1">
      <div class="btl-brite" style="font-size:32px;font-weight:800;letter-spacing:-0.045em;color:#fff">Brite</div>
      <div class="btl-sub" style="font-size:8px;font-weight:300;color:rgba(255,255,255,0.35);text-transform:uppercase;margin-top:3px;display:block">Tech Lifestyle</div>
    </div>
    <div style="width:1px;height:36px;background:rgba(255,255,255,0.1)"></div>
    <div>
      <div style="font-size:18px;font-weight:700;color:#fff;letter-spacing:-0.02em">Content Pipeline</div>
      <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-top:2px;font-style:italic">Technology, beautifully lived.</div>
    </div>
  </div>
  <div style="font-size:11px;color:rgba(255,255,255,0.3)">{now_utc.strftime("%d %b %Y  ·  %H:%M UTC")}</div>
</div>
""",
    unsafe_allow_html=True,
)

# Refresh button in its own row, clearly visible
col_r = st.columns([5, 1])
with col_r[1]:
    if st.button("↺  Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ── Pipeline flow ─────────────────────────────────────────────────────────────

STAGES = [
    ("Research", len(topics), "#6E6E73"),
    ("Pending", len(pending), "#F59E0B"),
    ("Approved", len(approved_t), "#0066CC"),
    ("Content", len(content_ready), "#8B5CF6"),
    ("Media", len(media_ready), "#EC4899"),
    ("Sched", len(scheduled), "#10B981"),
    ("Live", len(published), "#059669"),
    ("Failed", len(failed), "#EF4444"),
]

cols = st.columns(len(STAGES) * 2 - 1)
for i, (label, count, color) in enumerate(STAGES):
    with cols[i * 2]:
        st.markdown(
            f"""
        <div style="background:{color}12;border:2px solid {color}55;border-radius:12px;
                    padding:14px 4px;text-align:center;margin-bottom:8px">
          <div style="font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;
                      color:{color};margin-bottom:4px">{label}</div>
          <div style="font-size:34px;font-weight:800;letter-spacing:-0.04em;color:{color};line-height:1">{count}</div>
        </div>""",
            unsafe_allow_html=True,
        )
    if i < len(STAGES) - 1:
        with cols[i * 2 + 1]:
            st.markdown(
                "<div style='text-align:center;font-size:18px;color:#D1D5DB;padding-top:24px'>›</div>",
                unsafe_allow_html=True,
            )

st.markdown("<div style='margin-bottom:4px'></div>", unsafe_allow_html=True)

# ── Manual controls ───────────────────────────────────────────────────────────


_CMD_COOLDOWN_SECS = 10


def _queue_command(command: str, cooldown_key: str | None = None) -> None:
    """Insert a pipeline command for the worker to pick up within ~2 minutes.

    Enforces a per-key cooldown to prevent accidental or malicious cost spikes
    from rapid repeated button clicks.
    """
    key = f"_cmd_ts_{cooldown_key or command}"
    now = datetime.now(UTC).timestamp()
    if now - st.session_state.get(key, 0.0) < _CMD_COOLDOWN_SECS:
        raise RuntimeError("Too many requests — please wait a moment before trying again.")
    db.table("pipeline_commands").insert(
        {
            "command": command,
            "status": "pending",
            "requested_at": datetime.now(UTC).isoformat(),
        }
    ).execute()
    st.session_state[key] = now


with st.expander("⚡ Manual controls — run pipeline jobs now"):
    st.caption(
        "Commands are picked up by the worker within **2 minutes**. "
        "Refresh the page after that to see the results."
    )
    ctrl_c1, ctrl_c2, ctrl_c3 = st.columns(3)

    with ctrl_c1:
        if st.button("🖼 Generate missing images", use_container_width=True):
            try:
                _queue_command("image_refresh")
                st.success("✅ Queued — images will regenerate within 2 minutes.")
            except Exception:
                logger.exception("Failed to queue command")
                st.error("Failed to queue command — check server logs.")

    with ctrl_c2:
        if st.button("📤 Publish due posts", use_container_width=True):
            try:
                _queue_command("publish")
                st.success("✅ Queued — publisher will run within 2 minutes.")
            except Exception:
                logger.exception("Failed to queue command")
                st.error("Failed to queue command — check server logs.")

    with ctrl_c3:
        if st.button("⚡ Run everything now", use_container_width=True, type="primary"):
            try:
                _queue_command("all")
                st.success("✅ Queued — image refresh + publish will run within 2 minutes.")
            except Exception:
                logger.exception("Failed to queue command")
                st.error("Failed to queue command — check server logs.")

# ── Helpers ───────────────────────────────────────────────────────────────────

PLATFORM_COLORS = {
    "instagram": "#C2185B",  # deep pink
    "facebook": "#1565C0",  # deep blue
    "twitter": "#00897B",  # teal
    "linkedin": "#E65100",  # burnt orange
    "tiktok": "#43A047",  # medium green (bright enough to read at small sizes)
    "youtube": "#B71C1C",  # dark red
}


def _platform_pill(platform: str) -> str:
    color = PLATFORM_COLORS.get(platform.lower(), "#6E6E73")
    return (
        f"<span style='background:{color}18;color:{color};border-radius:9999px;"
        f"padding:2px 10px;font-size:11px;font-weight:700;letter-spacing:0.04em;"
        f"text-transform:uppercase'>{html.escape(platform)}</span>"
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
    plat_color = PLATFORM_COLORS.get(platform.lower(), "#6E6E73")
    sched_color = "#10B981" if time_label == "scheduled" else "#059669"
    sched_icon = "📅" if time_label == "scheduled" else "📢"

    # Escape all DB-sourced values before HTML interpolation (XSS prevention).
    e_platform = html.escape(str(platform))
    e_title = html.escape(str(title))
    e_pillar = html.escape(str(pillar))
    e_caption = html.escape(str(caption))

    # Image
    url = post.get("thumbnail_url", "")
    if url:
        st.image(url if url.endswith(".png") else url + ".png", use_container_width=True)
    else:
        st.markdown(
            "<div style='background:#F5F5F7;border-radius:8px;height:90px;display:flex;"
            "align-items:center;justify-content:center;color:#A1A1A6;font-size:12px'>"
            "No image</div>",
            unsafe_allow_html=True,
        )

    # Info block — all inline styles, immune to Streamlit theming
    pills = ""
    if platform:
        pills += (
            f"<span style='background:{plat_color}18;color:{plat_color};border-radius:9999px;"
            f"padding:2px 10px;font-size:11px;font-weight:700;letter-spacing:.04em;"
            f"text-transform:uppercase'>{e_platform}</span> "
        )
    if is_carousel:
        pills += f"<span style='color:#7C3AED;font-size:11px;font-weight:700'>🎠 {len(slides)} slides</span> "
    if time_str:
        pills += f"<span style='color:{sched_color};font-size:11px;font-weight:600'>{sched_icon} {html.escape(time_str)}</span>"

    st.markdown(
        f"""
    <div style='font-family:sans-serif;padding:6px 2px 4px'>
      <div style='margin-bottom:5px'>{pills}</div>
      <div style='font-size:14px;font-weight:700;color:#1D1D1F;line-height:1.35;margin-bottom:3px'>{e_title}</div>
      <div style='font-size:12px;color:#6E6E73'>{e_pillar}</div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    # Expandable caption / slides
    if is_carousel and slides:
        with st.expander(f"View {len(slides)} slides"):
            for j, slide in enumerate(slides):
                role = slide.get("role", "")
                tag = " (cover)" if role == "cover" else " (CTA)" if role == "cta" else ""
                e_headline = html.escape(str(slide.get("headline", "")))
                e_body = html.escape(str(slide.get("body", "")))
                st.markdown(
                    f"<div style='font-weight:700;color:#1D1D1F;font-size:13px'>{j + 1}. {e_headline}{tag}</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"<div style='font-size:12px;color:#6E6E73;margin-bottom:6px'>{e_body}</div>",
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
            "<div style='font-size:11px;color:#0066CC;margin-top:8px;line-height:1.8'>"
            + " ".join(f"#{html.escape(str(h))}" for h in hashtags)
            + "</div>"
            if hashtags
            else ""
        )
        st.markdown(
            f"""
        <details style="margin-top:8px;border:1px solid #E8E8ED;border-radius:10px;overflow:hidden">
          <summary style="padding:10px 14px;font-size:13px;font-weight:600;color:#1D1D1F;
                          background:#F5F5F7;cursor:pointer;list-style:none;
                          display:flex;align-items:center;justify-content:space-between">
            Caption &nbsp;›
          </summary>
          <div style="padding:12px 14px;font-size:13px;color:#1D1D1F;line-height:1.7;background:#fff">
            {e_caption}{tags_html}
          </div>
        </details>
        """,
            unsafe_allow_html=True,
        )


# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_topics, tab_posts, tab_scheduled, tab_calendar, tab_published = st.tabs(
    [
        f"Topics to Review  {len(pending)}",
        f"In Progress  {len(content_ready) + len(media_ready)}",
        f"Scheduled  {len(scheduled)}",
        "Calendar",
        f"Published  {len(published)}",
    ]
)

# ── Topics ────────────────────────────────────────────────────────────────────

with tab_topics:
    if not pending:
        st.info("✅  All clear — no topics awaiting review. Research agent runs daily at 05:30.")
    else:
        st.caption(f"{len(pending)} topic(s) awaiting approval.")
        for topic in pending:
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                with c1:
                    score = topic.get("relevance_score", 0)
                    sc = "#10B981" if score >= 80 else "#F59E0B" if score >= 60 else "#EF4444"
                    e_topic_title = html.escape(str(topic.get("title", "")))
                    st.markdown(
                        f"**{e_topic_title}** &nbsp;"
                        f"<span style='background:{sc}18;color:{sc};border-radius:9999px;"
                        f"padding:2px 10px;font-size:11px;font-weight:700'>Score {score}</span>"
                        f" &nbsp; {_platform_pill(topic.get('platform', ''))}",
                        unsafe_allow_html=True,
                    )
                    st.caption(f"**{topic.get('pillar', '—')}** | {topic.get('summary', '')}")
                    if topic.get("content_angle"):
                        st.markdown(f"*Angle:* {topic['content_angle']}")
                    if topic.get("rationale"):
                        st.markdown(f"*Why:* {topic['rationale']}")
                    for src in (topic.get("sources") or [])[:2]:
                        st.markdown(f"🔗 {src}")
                with c2:
                    tid = topic["id"]
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

# ── In Progress ───────────────────────────────────────────────────────────────

with tab_posts:
    items = content_ready + media_ready
    if not items:
        st.info("🖼️  Nothing in progress right now.")
    else:
        cols = st.columns(3)
        for i, post in enumerate(items):
            with cols[i % 3]:
                with st.container(border=True):
                    _post_card(post)
                    pid = post.get("id", "")
                    if pid and st.button(
                        "🗑 Dismiss", key=f"dismiss_prog_{pid}", use_container_width=True
                    ):
                        db.table("posts").update({"status": "dismissed"}).eq("id", pid).execute()
                        st.cache_data.clear()
                        st.rerun()

# ── Scheduled ─────────────────────────────────────────────────────────────────

with tab_scheduled:
    if not scheduled:
        st.info("📅  Nothing scheduled yet.")
    else:
        reg = sorted(
            [p for p in scheduled if p.get("post_type") != "carousel"],
            key=lambda p: p.get("scheduled_time") or "",
        )
        car = sorted(
            [p for p in scheduled if p.get("post_type") == "carousel"],
            key=lambda p: p.get("scheduled_time") or "",
        )
        if reg:
            st.markdown(
                "<div style='font-size:16px;font-weight:700;color:#1D1D1F;padding:8px 0 4px'>Regular Posts</div>",
                unsafe_allow_html=True,
            )
            cols = st.columns(3)
            for i, p in enumerate(reg):
                with cols[i % 3]:
                    with st.container(border=True):
                        _post_card(p, _sched_str(p), "scheduled")
                        pid = p.get("id", "")
                        btn_pub, btn_dis = st.columns(2)
                        with btn_pub:
                            if pid and st.button(
                                "📤 Publish now",
                                key=f"pub_sched_{pid}",
                                use_container_width=True,
                                type="primary",
                            ):
                                db.table("posts").update(
                                    {"scheduled_time": datetime.now(UTC).isoformat()}
                                ).eq("id", pid).execute()
                                try:
                                    _queue_command("publish", cooldown_key=f"pub_{pid}")
                                    st.success("Queued — will publish within 2 minutes.")
                                except RuntimeError as e:
                                    st.warning(str(e))
                        with btn_dis:
                            if pid and st.button(
                                "🗑 Dismiss",
                                key=f"dismiss_sched_{pid}",
                                use_container_width=True,
                            ):
                                db.table("posts").update({"status": "dismissed"}).eq(
                                    "id", pid
                                ).execute()
                                st.cache_data.clear()
                                st.rerun()
        if car:
            st.markdown(
                "<div style='font-size:16px;font-weight:700;color:#7C3AED;padding:16px 0 4px'>🎠 Carousels</div>",
                unsafe_allow_html=True,
            )
            cols = st.columns(3)
            for i, p in enumerate(car):
                with cols[i % 3]:
                    with st.container(border=True):
                        _post_card(p, _sched_str(p), "scheduled")
                        pid = p.get("id", "")
                        btn_pub, btn_dis = st.columns(2)
                        with btn_pub:
                            if pid and st.button(
                                "📤 Publish now",
                                key=f"pub_car_{pid}",
                                use_container_width=True,
                                type="primary",
                            ):
                                db.table("posts").update(
                                    {"scheduled_time": datetime.now(UTC).isoformat()}
                                ).eq("id", pid).execute()
                                try:
                                    _queue_command("publish", cooldown_key=f"pub_{pid}")
                                    st.success("Queued — will publish within 2 minutes.")
                                except RuntimeError as e:
                                    st.warning(str(e))
                        with btn_dis:
                            if pid and st.button(
                                "🗑 Dismiss",
                                key=f"dismiss_car_{pid}",
                                use_container_width=True,
                            ):
                                db.table("posts").update({"status": "dismissed"}).eq(
                                    "id", pid
                                ).execute()
                                st.cache_data.clear()
                                st.rerun()

# ── Calendar ──────────────────────────────────────────────────────────────────

with tab_calendar:
    # Build date → posts map
    all_active = [p for p in posts if p.get("status") not in ("failed", "draft")]
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

    # Month nav
    col_p, col_t, col_n = st.columns([1, 4, 1])
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
            f"<div style='font-size:24px;font-weight:800;letter-spacing:-0.03em;color:#1D1D1F;text-align:center;padding:6px 0'>{mname}</div>",
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
        f'<div style="text-align:center;font-size:11px;font-weight:700;letter-spacing:0.08em;'
        f'text-transform:uppercase;color:#A1A1A6;padding:8px 0">{d}</div>'
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
            border = "2px solid #0066CC" if is_today else "1px solid #E8E8ED"
            num_color = "#0066CC" if is_today else "#1D1D1F"
            dots = ""
            for p in day_posts[:8]:
                plat = (p.get("platform") or "").lower()
                col = PLATFORM_COLORS.get(plat, "#A1A1A6")
                dots += f'<div style="width:11px;height:11px;border-radius:50%;background:{col};flex-shrink:0;border:2px solid rgba(0,0,0,0.15)" title="{html.escape(str(p.get("topic", "")))}"></div>'
            count_html = (
                f'<div style="font-size:10px;font-weight:700;color:#0066CC;margin-top:3px">{len(day_posts)} post{"s" if len(day_posts) != 1 else ""}</div>'
                if day_posts
                else ""
            )
            cells_html += f"""
            <div style="background:#fff;border:{border};border-radius:10px;min-height:80px;padding:8px">
              <div style="font-size:13px;font-weight:700;color:{num_color};margin-bottom:4px">{day_num}</div>
              <div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:2px">{dots}</div>
              {count_html}
            </div>"""

    cal_html = f"""<!DOCTYPE html><html><head>
    <link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;700&display=swap" rel="stylesheet">
    <style>*{{margin:0;padding:0;box-sizing:border-box;font-family:'Figtree',sans-serif}}body{{background:#F5F5F7;padding:8px}}</style>
    </head><body>
    <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:5px">
      {header_html}{cells_html}
    </div>
    </body></html>"""

    components.html(cal_html, height=len(cal) * 95 + 52, scrolling=False)

    # Legend
    legend = " &nbsp;&nbsp; ".join(
        f'<span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;color:#6E6E73">'
        f'<span style="width:12px;height:12px;border-radius:50%;background:{c};display:inline-block;border:2px solid rgba(0,0,0,0.15)"></span>{p.title()}</span>'
        for p, c in PLATFORM_COLORS.items()
    )
    st.markdown(f"<div style='margin-top:12px'>{legend}</div>", unsafe_allow_html=True)

    # Day detail
    st.markdown("---")
    st.markdown("**View a specific day**")
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
                        status = p.get("status", "")
                        label = "scheduled" if status == "scheduled" else "published"
                        _post_card(p, _sched_str(p), label)
        else:
            st.caption(f"No posts on {sel_date.strftime('%A %d %B %Y')}.")
    except ValueError:
        st.warning("Invalid date.")

    # Monthly summary
    month_items = [
        p for d, ps in date_posts.items() for p in ps if d.year == year and d.month == month
    ]
    if month_items:
        from collections import Counter

        pcounts = Counter(p.get("platform", "").lower() for p in month_items)
        st.markdown(f"**{mname} — {len(month_items)} posts total**")
        scols = st.columns(len(pcounts))
        for i, (plat, cnt) in enumerate(sorted(pcounts.items(), key=lambda x: -x[1])):
            c = PLATFORM_COLORS.get(plat, "#6E6E73")
            with scols[i]:
                st.markdown(
                    f"""
                <div style="background:{c}12;border:2px solid {c}44;border-radius:12px;
                            padding:14px;text-align:center;margin-top:8px">
                  <div style="font-size:26px;font-weight:800;color:{c}">{cnt}</div>
                  <div style="font-size:10px;font-weight:700;letter-spacing:0.08em;
                              text-transform:uppercase;color:{c};margin-top:3px">{plat}</div>
                </div>""",
                    unsafe_allow_html=True,
                )

# ── Published ─────────────────────────────────────────────────────────────────

with tab_published:
    if not published:
        st.info("📢  Nothing published yet — posts will appear here once live.")
    else:
        cols = st.columns(3)
        for i, post in enumerate(published):
            with cols[i % 3]:
                with st.container(border=True):
                    _post_card(post, _sched_str(post), "published")

# ── Failed alert ──────────────────────────────────────────────────────────────

if failed:
    st.divider()
    with st.expander(f"⚠️  {len(failed)} Failed Post(s) — click to review"):
        col_retry_all, col_delete_all, _ = st.columns([1, 1, 3])
        with col_retry_all:
            if st.button("↩ Retry all", key="retry_all_failed", type="primary"):
                ids = [p["id"] for p in failed if p.get("id")]
                if ids:
                    db.table("posts").update({"status": "scheduled", "error": None}).in_(
                        "id", ids
                    ).execute()
                    st.success(
                        f"Reset {len(ids)} post(s) to scheduled — they'll publish at their next due time."
                    )
                    st.rerun()
        with col_delete_all:
            if st.button("🗑 Dismiss all", key="delete_all_failed"):
                ids = [p["id"] for p in failed if p.get("id")]
                if ids:
                    db.table("posts").update({"status": "dismissed"}).in_("id", ids).execute()
                    st.success(
                        f"Dismissed {len(ids)} failed post(s) — kept in Supabase, hidden here."
                    )
                    st.rerun()

        for post in failed:
            title = post.get("title") or post.get("topic", "Untitled")
            platform = post.get("platform", "—")
            detail = post.get("error") or "No detail"
            post_id = post.get("id", "")
            col_err, col_retry, col_del = st.columns([5, 1, 1])
            with col_err:
                st.error(f"**{title}** ({platform})  \n{detail}")
            with col_retry:
                if post_id and st.button("↩ Retry", key=f"retry_{post_id}"):
                    db.table("posts").update({"status": "scheduled", "error": None}).eq(
                        "id", post_id
                    ).execute()
                    st.success("Reset to scheduled.")
                    st.rerun()
            with col_del:
                if post_id and st.button("🗑 Dismiss", key=f"delete_{post_id}"):
                    db.table("posts").update({"status": "dismissed"}).eq("id", post_id).execute()
                    st.rerun()
