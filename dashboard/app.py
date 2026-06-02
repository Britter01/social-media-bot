"""Brite Tech Lifestyle — Automation Dashboard."""

from __future__ import annotations

import calendar
import os
from collections import defaultdict
from datetime import UTC, datetime, date

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Brite Tech Lifestyle — Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Brand CSS ─────────────────────────────────────────────────────────────────

st.markdown("""
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
/* ── Global ── */
html, body, [class*="css"] { font-family: 'Figtree', sans-serif !important; }
.stApp { background: #F5F5F7; }

/* ── Hide Streamlit chrome ── */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding-top: 0 !important; max-width: 1400px; }

/* ── Top nav bar ── */
.btl-nav {
    background: #000;
    padding: 0 48px;
    height: 64px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: -1rem -1rem 0 -1rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
}
.btl-logo-wrap { display: flex; flex-direction: column; line-height: 1; }
.btl-logo { font-size: 26px; font-weight: 800; letter-spacing: -0.045em; color: #fff; }
.btl-logo-sub { font-size: 9px; font-weight: 300; letter-spacing: 0.22em; color: rgba(255,255,255,0.4); text-transform: uppercase; margin-top: 2px; }
.btl-nav-right { font-size: 12px; color: rgba(255,255,255,0.35); font-weight: 300; letter-spacing: 0.04em; }

/* ── Page header ── */
.btl-page-header {
    background: #fff;
    border-radius: 16px;
    padding: 28px 36px;
    margin: 20px 0 20px 0;
    border: 1px solid #E8E8ED;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.btl-page-title { font-size: 28px; font-weight: 800; letter-spacing: -0.04em; color: #1D1D1F; margin: 0; }
.btl-page-sub { font-size: 14px; font-weight: 300; color: #6E6E73; margin: 4px 0 0 0; }

/* ── Stat cards ── */
.btl-stat {
    background: #fff;
    border-radius: 16px;
    padding: 20px 24px;
    border: 1px solid #E8E8ED;
    text-align: center;
    transition: transform 0.2s;
}
.btl-stat:hover { transform: translateY(-2px); }
.btl-stat-num { font-size: 40px; font-weight: 800; letter-spacing: -0.04em; line-height: 1; }
.btl-stat-label { font-size: 11px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: #A1A1A6; margin-top: 6px; }

/* ── Pipeline flow ── */
.btl-pipe-card {
    background: #fff;
    border-radius: 12px;
    padding: 16px 8px;
    text-align: center;
    border: 2px solid #E8E8ED;
    transition: border-color 0.2s;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background: #fff;
    border-radius: 12px;
    padding: 4px;
    border: 1px solid #E8E8ED;
    gap: 2px;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 8px;
    font-family: 'Figtree', sans-serif !important;
    font-weight: 600;
    font-size: 13px;
    color: #6E6E73;
    padding: 8px 18px;
    background: transparent;
}
.stTabs [aria-selected="true"] {
    background: #0066CC !important;
    color: #fff !important;
}
.stTabs [data-baseweb="tab-border"] { display: none; }

/* ── Buttons ── */
.stButton > button {
    font-family: 'Figtree', sans-serif !important;
    font-weight: 600;
    border-radius: 9999px;
    border: 1px solid #E8E8ED;
    background: #fff;
    color: #1D1D1F;
    transition: all 0.2s;
}
.stButton > button:hover { border-color: #0066CC; color: #0066CC; }
.stButton > button[kind="primary"] {
    background: #0066CC !important;
    color: #fff !important;
    border-color: #0066CC !important;
}
.stButton > button[kind="primary"]:hover { background: #004999 !important; }

/* ── Cards / containers ── */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 16px !important;
    border-color: #E8E8ED !important;
    background: #fff;
    overflow: hidden;
}

/* ── Calendar ── */
.cal-grid {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 4px;
    margin-top: 8px;
}
.cal-header-cell {
    text-align: center;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #A1A1A6;
    padding: 8px 0;
}
.cal-day {
    background: #fff;
    border: 1px solid #E8E8ED;
    border-radius: 10px;
    min-height: 72px;
    padding: 8px;
    position: relative;
    transition: border-color 0.2s;
}
.cal-day:hover { border-color: #0066CC; }
.cal-day.empty { background: transparent; border-color: transparent; }
.cal-day.today { border-color: #0066CC; border-width: 2px; }
.cal-day-num {
    font-size: 13px;
    font-weight: 700;
    color: #1D1D1F;
    margin-bottom: 4px;
}
.cal-day.today .cal-day-num { color: #0066CC; }
.cal-dot-row { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 4px; }
.cal-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
}
.cal-dot.instagram { background: #E1306C; }
.cal-dot.facebook { background: #1877F2; }
.cal-dot.twitter { background: #1DA1F2; }
.cal-dot.linkedin { background: #0A66C2; }
.cal-dot.tiktok { background: #010101; }
.cal-dot.youtube { background: #FF0000; }
.cal-count {
    font-size: 10px;
    font-weight: 700;
    color: #0066CC;
    margin-top: 2px;
}
.cal-month-nav {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 16px;
}
.cal-month-title {
    font-size: 22px;
    font-weight: 800;
    letter-spacing: -0.03em;
    color: #1D1D1F;
    min-width: 200px;
}
.platform-legend {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 16px;
}
.legend-item {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 500;
    color: #6E6E73;
}
</style>
""", unsafe_allow_html=True)

# ── Authentication ─────────────────────────────────────────────────────────────

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

    # Branded login screen
    st.markdown("""
    <div style="max-width:400px;margin:80px auto 0;text-align:center">
        <div style="font-size:52px;font-weight:800;letter-spacing:-0.045em;color:#1D1D1F;line-height:1">Brite</div>
        <div style="font-size:11px;font-weight:300;letter-spacing:0.22em;color:#A1A1A6;text-transform:uppercase;margin-top:4px;margin-bottom:32px">Tech Lifestyle</div>
        <div style="font-size:16px;font-weight:500;color:#6E6E73;margin-bottom:24px">Automation Dashboard</div>
    </div>
    """, unsafe_allow_html=True)

    col = st.columns([1, 2, 1])[1]
    with col:
        pwd = st.text_input("Password", type="password", placeholder="Enter dashboard password", label_visibility="collapsed")
        if st.button("Sign In", use_container_width=True, type="primary"):
            if pwd == expected:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
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

@st.cache_data(ttl=30)
def load_topics():
    return db.table("topics").select("*").order("relevance_score", desc=True).limit(200).execute().data or []

@st.cache_data(ttl=30)
def load_posts():
    return db.table("posts").select("*").order("scheduled_time", desc=False).limit(500).execute().data or []

topics = load_topics()
posts  = load_posts()

def by_status(items, status):
    return [i for i in items if i.get("status") == status]

pending       = by_status(topics, "pending_approval")
approved_t    = by_status(topics, "approved")
used          = by_status(topics, "used")
rejected      = by_status(topics, "rejected")

drafts        = by_status(posts, "draft")
content_ready = by_status(posts, "content_ready")
media_ready   = by_status(posts, "media_ready")
scheduled     = by_status(posts, "scheduled")
published     = by_status(posts, "published")
failed        = by_status(posts, "failed")

# ── Top nav bar ───────────────────────────────────────────────────────────────

now_utc = datetime.now(UTC)
now_str = now_utc.strftime("%d %b %Y  %H:%M UTC")

st.markdown(f"""
<div class="btl-nav">
    <div class="btl-logo-wrap">
        <span class="btl-logo">Brite</span>
        <span class="btl-logo-sub">Tech Lifestyle</span>
    </div>
    <div class="btl-nav-right">Automation Dashboard &nbsp;·&nbsp; {now_str}</div>
</div>
""", unsafe_allow_html=True)

# ── Page header + refresh ─────────────────────────────────────────────────────

col_hdr, col_btn = st.columns([5, 1])
with col_hdr:
    st.markdown("""
    <div style="padding: 24px 0 8px 0">
        <div style="font-size:30px;font-weight:800;letter-spacing:-0.04em;color:#1D1D1F">
            Content Pipeline
        </div>
        <div style="font-size:14px;font-weight:300;color:#6E6E73;margin-top:4px">
            Technology, beautifully lived.
        </div>
    </div>
    """, unsafe_allow_html=True)
with col_btn:
    st.markdown("<div style='padding-top:28px'>", unsafe_allow_html=True)
    if st.button("↺ Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

# ── Pipeline flow ─────────────────────────────────────────────────────────────

STAGES = [
    ("Research",        len(topics),                   "#6E6E73"),
    ("Pending",         len(pending),                  "#F59E0B"),
    ("Approved",        len(approved_t),               "#0066CC"),
    ("Content Ready",   len(content_ready),            "#8B5CF6"),
    ("Media Ready",     len(media_ready),              "#EC4899"),
    ("Scheduled",       len(scheduled),                "#10B981"),
    ("Published",       len(published),                "#059669"),
    ("Failed",          len(failed),                   "#EF4444"),
]

cols = st.columns(len(STAGES) * 2 - 1)
for i, (label, count, color) in enumerate(STAGES):
    with cols[i * 2]:
        st.markdown(f"""
        <div style="background:{color}12;border:2px solid {color}44;border-radius:14px;
                    padding:16px 6px;text-align:center;margin-bottom:8px">
            <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;
                        text-transform:uppercase;color:{color};margin-bottom:6px">{label}</div>
            <div style="font-size:36px;font-weight:800;letter-spacing:-0.04em;color:{color};
                        line-height:1">{count}</div>
        </div>
        """, unsafe_allow_html=True)
    if i < len(STAGES) - 1:
        with cols[i * 2 + 1]:
            st.markdown(
                "<div style='text-align:center;font-size:20px;color:#D1D5DB;padding-top:26px'>›</div>",
                unsafe_allow_html=True,
            )

st.markdown("<div style='margin-bottom:8px'></div>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_topics, tab_posts, tab_scheduled, tab_calendar, tab_published = st.tabs([
    f"Topics to Review  {len(pending)}",
    f"In Progress  {len(content_ready) + len(media_ready)}",
    f"Scheduled  {len(scheduled)}",
    "Calendar",
    f"Published  {len(published)}",
])

# ── Helpers ───────────────────────────────────────────────────────────────────

PLATFORM_COLORS = {
    "instagram": "#E1306C",
    "facebook":  "#1877F2",
    "twitter":   "#1DA1F2",
    "linkedin":  "#0A66C2",
    "tiktok":    "#010101",
    "youtube":   "#FF0000",
}

def _platform_pill(platform: str) -> str:
    color = PLATFORM_COLORS.get(platform.lower(), "#6E6E73")
    return (
        f"<span style='background:{color}18;color:{color};border-radius:9999px;"
        f"padding:3px 10px;font-size:11px;font-weight:700;letter-spacing:0.04em;"
        f"text-transform:uppercase'>{platform}</span>"
    )

def _sched_str(p):
    sched = p.get("scheduled_time", "")
    try:
        dt = datetime.fromisoformat(sched.replace("Z", "+00:00"))
        return dt.strftime("%a %d %b · %H:%M")
    except Exception:
        return sched

def _post_card(post: dict, time_str: str = "", time_label: str = "") -> None:
    is_carousel = post.get("post_type") == "carousel"
    slides = post.get("slides") or []
    platform = post.get("platform", "")

    if post.get("thumbnail_url"):
        url = post["thumbnail_url"]
        if not url.endswith(".png"):
            url += ".png"
        st.image(url, use_container_width=True)
    else:
        st.markdown(
            "<div style='background:#F5F5F7;border-radius:8px;height:120px;"
            "display:flex;align-items:center;justify-content:center;"
            "color:#A1A1A6;font-size:13px'>No thumbnail</div>",
            unsafe_allow_html=True,
        )

    meta_parts = [_platform_pill(platform)] if platform else []
    if is_carousel:
        meta_parts.append(f"<span style='color:#7C3AED;font-size:11px;font-weight:700'>🎠 Carousel · {len(slides)} slides</span>")
    if time_str:
        color = "#10B981" if time_label == "scheduled" else "#059669"
        meta_parts.append(f"<span style='color:{color};font-size:11px;font-weight:600'>{'📅' if time_label=='scheduled' else '📢'} {time_str}</span>")

    if meta_parts:
        st.markdown(" &nbsp; ".join(meta_parts), unsafe_allow_html=True)

    st.markdown(f"**{post.get('title') or post.get('topic') or 'Untitled'}**")
    st.caption(post.get("pillar", "—"))

    if is_carousel and slides:
        with st.expander(f"View {len(slides)} slides"):
            for j, slide in enumerate(slides):
                role = slide.get("role", "")
                role_tag = " *(cover)*" if role == "cover" else " *(CTA)*" if role == "cta" else ""
                st.markdown(f"**{j+1}. {slide.get('headline','')}**{role_tag}")
                st.caption(slide.get("body", ""))
                if slide.get("image_url"):
                    st.image(slide["image_url"] + (".png" if not slide["image_url"].endswith(".png") else ""), use_container_width=True)
                st.divider()
    elif post.get("caption"):
        with st.expander("Caption"):
            st.write(post["caption"])
            if post.get("hashtags"):
                st.caption(" ".join(f"#{h}" for h in post["hashtags"]))

    if post.get("platform_post_id") and post["platform_post_id"] != "dry-run":
        st.caption(f"Post ID: `{post['platform_post_id']}`")

# ── Tab: Topics ───────────────────────────────────────────────────────────────

with tab_topics:
    if not pending:
        st.markdown("""
        <div style="background:#fff;border-radius:16px;padding:40px;text-align:center;border:1px solid #E8E8ED;margin-top:16px">
            <div style="font-size:32px;margin-bottom:12px">✅</div>
            <div style="font-size:16px;font-weight:600;color:#1D1D1F">All clear</div>
            <div style="font-size:14px;color:#6E6E73;margin-top:4px">No topics awaiting review. The research agent runs daily at 05:30.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f"<div style='padding:12px 0;font-size:14px;color:#6E6E73'>{len(pending)} topic(s) awaiting your approval.</div>", unsafe_allow_html=True)
        for topic in pending:
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                with c1:
                    score = topic.get("relevance_score", 0)
                    score_color = "#10B981" if score >= 80 else "#F59E0B" if score >= 60 else "#EF4444"
                    st.markdown(
                        f"**{topic['title']}** &nbsp;"
                        f"<span style='background:{score_color}18;color:{score_color};"
                        f"border-radius:9999px;padding:3px 10px;font-size:11px;font-weight:700'>"
                        f"Score {score}</span> &nbsp;"
                        + _platform_pill(topic.get("platform", "")),
                        unsafe_allow_html=True,
                    )
                    st.caption(f"**{topic.get('pillar','—')}** | {topic.get('summary','')}")
                    if topic.get("content_angle"):
                        st.markdown(f"*Angle:* {topic['content_angle']}")
                    if topic.get("rationale"):
                        st.markdown(f"*Why:* {topic['rationale']}")
                    for src in (topic.get("sources") or [])[:2]:
                        st.markdown(f"🔗 {src}")
                with c2:
                    tid = topic["id"]
                    if st.button("Approve", key=f"approve_{tid}", use_container_width=True, type="primary"):
                        db.table("topics").update({"status": "approved"}).eq("id", tid).execute()
                        st.cache_data.clear()
                        st.rerun()
                    if st.button("Reject", key=f"reject_{tid}", use_container_width=True):
                        db.table("topics").update({"status": "rejected"}).eq("id", tid).execute()
                        st.cache_data.clear()
                        st.rerun()

# ── Tab: In Progress ──────────────────────────────────────────────────────────

with tab_posts:
    in_progress = content_ready + media_ready
    if not in_progress:
        st.markdown("""
        <div style="background:#fff;border-radius:16px;padding:40px;text-align:center;border:1px solid #E8E8ED;margin-top:16px">
            <div style="font-size:32px;margin-bottom:12px">🖼️</div>
            <div style="font-size:16px;font-weight:600;color:#1D1D1F">Nothing in progress</div>
            <div style="font-size:14px;color:#6E6E73;margin-top:4px">Posts will appear here while content and media are being generated.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        cols = st.columns(3)
        for i, post in enumerate(in_progress):
            with cols[i % 3]:
                with st.container(border=True):
                    _post_card(post)

# ── Tab: Scheduled ────────────────────────────────────────────────────────────

with tab_scheduled:
    if not scheduled:
        st.markdown("""
        <div style="background:#fff;border-radius:16px;padding:40px;text-align:center;border:1px solid #E8E8ED;margin-top:16px">
            <div style="font-size:32px;margin-bottom:12px">📅</div>
            <div style="font-size:16px;font-weight:600;color:#1D1D1F">Nothing scheduled yet</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        regular_sched  = sorted([p for p in scheduled if p.get("post_type") != "carousel"], key=lambda p: p.get("scheduled_time") or "")
        carousel_sched = sorted([p for p in scheduled if p.get("post_type") == "carousel"],  key=lambda p: p.get("scheduled_time") or "")

        if regular_sched:
            st.markdown("<div style='font-size:16px;font-weight:700;color:#1D1D1F;padding:12px 0 8px'>Regular Posts</div>", unsafe_allow_html=True)
            cols = st.columns(3)
            for i, post in enumerate(regular_sched):
                with cols[i % 3]:
                    with st.container(border=True):
                        _post_card(post, _sched_str(post), "scheduled")

        if carousel_sched:
            st.markdown("<div style='font-size:16px;font-weight:700;color:#7C3AED;padding:16px 0 8px'>🎠 Carousels</div>", unsafe_allow_html=True)
            cols = st.columns(3)
            for i, post in enumerate(carousel_sched):
                with cols[i % 3]:
                    with st.container(border=True):
                        _post_card(post, _sched_str(post), "scheduled")

# ── Tab: Calendar ─────────────────────────────────────────────────────────────

with tab_calendar:

    # Build date → posts lookup from ALL non-failed posts
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

    # Month navigation
    today = datetime.now(UTC).date()
    if "cal_year" not in st.session_state:
        st.session_state.cal_year  = today.year
        st.session_state.cal_month = today.month

    col_prev, col_title, col_next = st.columns([1, 4, 1])
    with col_prev:
        st.markdown("<div style='padding-top:8px'>", unsafe_allow_html=True)
        if st.button("← Prev", use_container_width=True):
            if st.session_state.cal_month == 1:
                st.session_state.cal_month = 12
                st.session_state.cal_year -= 1
            else:
                st.session_state.cal_month -= 1
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
    with col_title:
        month_name = datetime(st.session_state.cal_year, st.session_state.cal_month, 1).strftime("%B %Y")
        st.markdown(f"<div style='font-size:26px;font-weight:800;letter-spacing:-0.03em;color:#1D1D1F;text-align:center;padding:8px 0'>{month_name}</div>", unsafe_allow_html=True)
    with col_next:
        st.markdown("<div style='padding-top:8px'>", unsafe_allow_html=True)
        if st.button("Next →", use_container_width=True):
            if st.session_state.cal_month == 12:
                st.session_state.cal_month = 1
                st.session_state.cal_year += 1
            else:
                st.session_state.cal_month += 1
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    # Platform legend
    st.markdown("""
    <div class="platform-legend">
        <div class="legend-item"><div class="cal-dot instagram"></div>Instagram</div>
        <div class="legend-item"><div class="cal-dot facebook"></div>Facebook</div>
        <div class="legend-item"><div class="cal-dot twitter"></div>Twitter / X</div>
        <div class="legend-item"><div class="cal-dot linkedin"></div>LinkedIn</div>
        <div class="legend-item"><div class="cal-dot tiktok"></div>TikTok</div>
        <div class="legend-item"><div class="cal-dot youtube"></div>YouTube</div>
    </div>
    """, unsafe_allow_html=True)

    # Build calendar grid HTML
    year  = st.session_state.cal_year
    month = st.session_state.cal_month
    cal   = calendar.monthcalendar(year, month)
    days  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    header_html = "".join(f'<div class="cal-header-cell">{d}</div>' for d in days)

    cells_html = ""
    for week in cal:
        for day_num in week:
            if day_num == 0:
                cells_html += '<div class="cal-day empty"></div>'
                continue

            d = date(year, month, day_num)
            is_today = (d == today)
            day_posts = date_posts.get(d, [])

            today_cls = " today" if is_today else ""
            dots = ""
            for p in day_posts[:8]:
                plat = (p.get("platform") or "").lower()
                dots += f'<div class="cal-dot {plat}" title="{p.get("topic","")}"></div>'

            count_html = f'<div class="cal-count">{len(day_posts)} post{"s" if len(day_posts)!=1 else ""}</div>' if day_posts else ""

            cells_html += f"""
            <div class="cal-day{today_cls}">
                <div class="cal-day-num">{day_num}</div>
                <div class="cal-dot-row">{dots}</div>
                {count_html}
            </div>"""

    cal_html = f"""
    <html><head>
    <link href="https://fonts.googleapis.com/css2?family=Figtree:wght@300;500;600;700;800&display=swap" rel="stylesheet">
    <style>
    * {{ margin:0; padding:0; box-sizing:border-box; font-family:'Figtree',sans-serif; }}
    body {{ background:#F5F5F7; padding:8px; }}
    .cal-grid {{ display:grid; grid-template-columns:repeat(7,1fr); gap:4px; }}
    .cal-header-cell {{ text-align:center; font-size:11px; font-weight:700; letter-spacing:0.08em; text-transform:uppercase; color:#A1A1A6; padding:8px 0; }}
    .cal-day {{ background:#fff; border:1px solid #E8E8ED; border-radius:10px; min-height:76px; padding:8px; }}
    .cal-day.empty {{ background:transparent; border-color:transparent; }}
    .cal-day.today {{ border-color:#0066CC; border-width:2px; }}
    .cal-day-num {{ font-size:13px; font-weight:700; color:#1D1D1F; margin-bottom:4px; }}
    .cal-day.today .cal-day-num {{ color:#0066CC; }}
    .cal-dot-row {{ display:flex; flex-wrap:wrap; gap:3px; margin-top:4px; }}
    .cal-dot {{ width:8px; height:8px; border-radius:50%; flex-shrink:0; }}
    .cal-dot.instagram {{ background:#E1306C; }}
    .cal-dot.facebook {{ background:#1877F2; }}
    .cal-dot.twitter {{ background:#1DA1F2; }}
    .cal-dot.linkedin {{ background:#0A66C2; }}
    .cal-dot.tiktok {{ background:#010101; }}
    .cal-dot.youtube {{ background:#FF0000; }}
    .cal-count {{ font-size:10px; font-weight:700; color:#0066CC; margin-top:3px; }}
    </style></head>
    <body>
    <div class="cal-grid">{header_html}{cells_html}</div>
    </body></html>
    """
    cal_rows = len(cal)
    components.html(cal_html, height=cal_rows * 92 + 48, scrolling=False)

    # Day detail — click a date to see posts
    st.markdown("<div style='margin-top:24px;font-size:16px;font-weight:700;color:#1D1D1F'>View a Day</div>", unsafe_allow_html=True)
    col_d, col_m, col_y = st.columns([1, 1, 1])
    with col_d:
        sel_day = st.number_input("Day", min_value=1, max_value=31, value=today.day, label_visibility="collapsed")
    with col_m:
        sel_month_name = st.selectbox("Month", options=list(calendar.month_name)[1:], index=month-1, label_visibility="collapsed")
        sel_month = list(calendar.month_name).index(sel_month_name)
    with col_y:
        sel_year = st.number_input("Year", min_value=2026, max_value=2030, value=year, label_visibility="collapsed")

    try:
        sel_date = date(sel_year, sel_month, sel_day)
        day_detail = date_posts.get(sel_date, [])
        if day_detail:
            st.markdown(f"<div style='font-size:14px;color:#6E6E73;margin:8px 0'>{len(day_detail)} post(s) on {sel_date.strftime('%A %d %B %Y')}</div>", unsafe_allow_html=True)
            dcols = st.columns(min(len(day_detail), 3))
            for i, p in enumerate(day_detail):
                with dcols[i % 3]:
                    with st.container(border=True):
                        _post_card(p, _sched_str(p), "scheduled" if p.get("status")=="scheduled" else "published")
        else:
            st.markdown(f"<div style='font-size:14px;color:#A1A1A6;margin:8px 0'>No posts on {sel_date.strftime('%A %d %B %Y')}.</div>", unsafe_allow_html=True)
    except ValueError:
        st.warning("Invalid date selected.")

    # Summary stats for the month
    month_posts = [p for d, ps in date_posts.items() for p in ps if d.year == year and d.month == month]
    if month_posts:
        from collections import Counter
        plat_counts = Counter(p.get("platform","").lower() for p in month_posts)
        st.markdown(f"<div style='margin-top:24px;font-size:16px;font-weight:700;color:#1D1D1F'>{month_name} Summary</div>", unsafe_allow_html=True)
        scols = st.columns(len(plat_counts) or 1)
        for i, (plat, cnt) in enumerate(sorted(plat_counts.items(), key=lambda x: -x[1])):
            color = PLATFORM_COLORS.get(plat, "#6E6E73")
            with scols[i]:
                st.markdown(f"""
                <div style="background:{color}10;border:2px solid {color}44;border-radius:12px;
                            padding:16px;text-align:center">
                    <div style="font-size:28px;font-weight:800;color:{color}">{cnt}</div>
                    <div style="font-size:11px;font-weight:700;letter-spacing:0.08em;
                                text-transform:uppercase;color:{color};margin-top:4px">{plat}</div>
                </div>
                """, unsafe_allow_html=True)

# ── Tab: Published ────────────────────────────────────────────────────────────

with tab_published:
    if not published:
        st.markdown("""
        <div style="background:#fff;border-radius:16px;padding:40px;text-align:center;border:1px solid #E8E8ED;margin-top:16px">
            <div style="font-size:32px;margin-bottom:12px">📢</div>
            <div style="font-size:16px;font-weight:600;color:#1D1D1F">Nothing published yet</div>
            <div style="font-size:14px;color:#6E6E73;margin-top:4px">Posts will appear here once they've gone live.</div>
        </div>
        """, unsafe_allow_html=True)
    else:
        cols = st.columns(3)
        for i, post in enumerate(published):
            pub = post.get("published_time", "")
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                pub_str = dt.strftime("%a %d %b · %H:%M")
            except Exception:
                pub_str = pub
            with cols[i % 3]:
                with st.container(border=True):
                    _post_card(post, pub_str, "published")

# ── Failed posts alert ────────────────────────────────────────────────────────

if failed:
    st.divider()
    with st.expander(f"⚠️  {len(failed)} Failed Post(s) — click to review", expanded=False):
        for post in failed:
            st.error(
                f"**{post.get('title') or post.get('topic','Untitled')}** "
                f"({post.get('platform','—')})  \n"
                f"{post.get('error') or 'No error detail'}"
            )
