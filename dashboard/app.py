"""Brite Tech Lifestyle — Automation Dashboard."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import streamlit as st
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# --- Page config -------------------------------------------------------------

st.set_page_config(
    page_title="Brite Tech Lifestyle — Dashboard",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# --- Supabase ----------------------------------------------------------------

@st.cache_resource
def get_db():
    # Streamlit Cloud exposes secrets via st.secrets; local dev uses .env
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

# --- Data --------------------------------------------------------------------

@st.cache_data(ttl=30)
def load_topics():
    return db.table("topics").select("*").order("relevance_score", desc=True).limit(200).execute().data or []

@st.cache_data(ttl=30)
def load_posts():
    return db.table("posts").select("*").order("created_at", desc=True).limit(200).execute().data or []

topics = load_topics()
posts  = load_posts()

def by_status(items, status):
    return [i for i in items if i.get("status") == status]

pending   = by_status(topics, "pending_approval")
approved  = by_status(topics, "approved")
used      = by_status(topics, "used")
rejected  = by_status(topics, "rejected")

drafts        = by_status(posts, "draft")
content_ready = by_status(posts, "content_ready")
media_ready   = by_status(posts, "media_ready")
scheduled     = by_status(posts, "scheduled")
published     = by_status(posts, "published")
failed        = by_status(posts, "failed")

# --- Header ------------------------------------------------------------------

st.markdown(
    "<h1 style='margin-bottom:0'>⚡ Brite Tech Lifestyle</h1>"
    "<p style='color:#6B7280;margin-top:0'>Automation Dashboard</p>",
    unsafe_allow_html=True,
)

now_str = datetime.now(UTC).strftime("%d %b %Y  %H:%M UTC")
col_title, col_refresh = st.columns([6, 1])
with col_refresh:
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption(now_str)

st.divider()

# --- Pipeline flow diagram ---------------------------------------------------

st.subheader("Pipeline Flow")

STAGES = [
    ("🔍", "Research",        len(topics),        "#6B7280"),
    ("⏳", "Pending Approval", len(pending),        "#F59E0B"),
    ("✅", "Approved",         len(approved),       "#3B82F6"),
    ("✍️", "Content Ready",   len(content_ready),  "#8B5CF6"),
    ("🖼️", "Media Ready",     len(media_ready),    "#EC4899"),
    ("📅", "Scheduled",        len(scheduled),      "#10B981"),
    ("📢", "Published",        len(published),      "#059669"),
    ("❌", "Failed",           len(failed),         "#EF4444"),
]

cols = st.columns(len(STAGES) * 2 - 1)
for i, (icon, label, count, color) in enumerate(STAGES):
    with cols[i * 2]:
        st.markdown(
            f"""
            <div style="background:{color}18;border:2px solid {color};border-radius:12px;
                        padding:14px 6px;text-align:center;">
                <div style="font-size:22px">{icon}</div>
                <div style="font-size:11px;color:{color};font-weight:700;
                            line-height:1.2;margin:4px 0">{label}</div>
                <div style="font-size:32px;font-weight:800;color:{color};
                            line-height:1">{count}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    if i < len(STAGES) - 1:
        with cols[i * 2 + 1]:
            st.markdown(
                "<div style='text-align:center;font-size:24px;color:#9CA3AF;"
                "padding-top:28px'>→</div>",
                unsafe_allow_html=True,
            )

st.divider()

# --- Tabs --------------------------------------------------------------------

tab_topics, tab_posts, tab_scheduled, tab_published = st.tabs([
    f"⏳ Topics to Review ({len(pending)})",
    f"🖼️  Posts ({len(content_ready) + len(media_ready)})",
    f"📅 Scheduled ({len(scheduled)})",
    f"📢 Published ({len(published)})",
])

# --- Topics to Review --------------------------------------------------------

with tab_topics:
    if not pending:
        st.info("No topics awaiting review. The research agent runs daily at 05:30.")
    else:
        st.caption(f"{len(pending)} topic(s) awaiting your approval before content is generated.")
        for topic in pending:
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                with c1:
                    score_color = "#10B981" if topic["relevance_score"] >= 80 else "#F59E0B"
                    st.markdown(
                        f"**{topic['title']}** &nbsp;"
                        f"<span style='background:{score_color}22;color:{score_color};"
                        f"border-radius:4px;padding:2px 8px;font-size:12px;font-weight:700'>"
                        f"Score {topic['relevance_score']}</span>",
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        f"**{topic.get('pillar','—')}** → {topic.get('platform','—')}  "
                        f"| {topic.get('summary','')}"
                    )
                    if topic.get("content_angle"):
                        st.markdown(f"*Angle:* {topic['content_angle']}")
                    if topic.get("rationale"):
                        st.markdown(f"*Why:* {topic['rationale']}")
                    sources = topic.get("sources") or []
                    if sources:
                        for src in sources[:2]:
                            st.markdown(f"🔗 {src}")
                with c2:
                    tid = topic["id"]
                    if st.button("✅ Approve", key=f"approve_{tid}", use_container_width=True, type="primary"):
                        db.table("topics").update({"status": "approved"}).eq("id", tid).execute()
                        st.cache_data.clear()
                        st.rerun()
                    if st.button("❌ Reject", key=f"reject_{tid}", use_container_width=True):
                        db.table("topics").update({"status": "rejected"}).eq("id", tid).execute()
                        st.cache_data.clear()
                        st.rerun()

# --- Posts (content/media ready) --------------------------------------------

with tab_posts:
    in_progress = content_ready + media_ready
    if not in_progress:
        st.info("No posts currently being processed.")
    else:
        cols = st.columns(3)
        for i, post in enumerate(in_progress):
            with cols[i % 3]:
                with st.container(border=True):
                    if post.get("thumbnail_url"):
                        st.image(post["thumbnail_url"], use_container_width=True)
                    else:
                        st.markdown(
                            "<div style='background:#F3F4F6;border-radius:8px;"
                            "height:140px;display:flex;align-items:center;"
                            "justify-content:center;color:#9CA3AF;font-size:13px'>"
                            "No thumbnail yet</div>",
                            unsafe_allow_html=True,
                        )
                    status = post.get("status", "")
                    status_color = "#8B5CF6" if status == "content_ready" else "#EC4899"
                    st.markdown(
                        f"<span style='background:{status_color}22;color:{status_color};"
                        f"border-radius:4px;padding:2px 8px;font-size:11px;font-weight:700'>"
                        f"{status.replace('_',' ').title()}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**{post.get('title') or post.get('topic','Untitled')}**")
                    st.caption(f"{post.get('pillar','—')} · {post.get('platform','—')}")
                    if post.get("caption"):
                        with st.expander("Caption"):
                            st.write(post["caption"])
                            if post.get("hashtags"):
                                st.caption(" ".join(f"#{h}" for h in post["hashtags"]))

# --- Scheduled ---------------------------------------------------------------

with tab_scheduled:
    if not scheduled:
        st.info("No posts currently scheduled.")
    else:
        cols = st.columns(3)
        for i, post in enumerate(sorted(scheduled, key=lambda p: p.get("scheduled_time") or "")):
            with cols[i % 3]:
                with st.container(border=True):
                    if post.get("thumbnail_url"):
                        st.image(post["thumbnail_url"], use_container_width=True)
                    sched = post.get("scheduled_time", "")
                    if sched:
                        try:
                            dt = datetime.fromisoformat(sched.replace("Z", "+00:00"))
                            sched_str = dt.strftime("%a %d %b · %H:%M %Z")
                        except Exception:
                            sched_str = sched
                    else:
                        sched_str = "—"
                    st.markdown(
                        f"<div style='background:#10B98122;color:#10B981;border-radius:4px;"
                        f"padding:4px 8px;font-size:12px;font-weight:700;text-align:center'>"
                        f"📅 {sched_str}</div>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**{post.get('title') or post.get('topic','Untitled')}**")
                    st.caption(f"{post.get('pillar','—')} · {post.get('platform','—')}")
                    if post.get("caption"):
                        with st.expander("Caption"):
                            st.write(post["caption"])

# --- Published ---------------------------------------------------------------

with tab_published:
    if not published:
        st.info("Nothing published yet. Set DRY_RUN=false in Railway when you're ready to go live.")
    else:
        cols = st.columns(3)
        for i, post in enumerate(published):
            with cols[i % 3]:
                with st.container(border=True):
                    if post.get("thumbnail_url"):
                        st.image(post["thumbnail_url"], use_container_width=True)
                    pub = post.get("published_time", "")
                    if pub:
                        try:
                            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                            pub_str = dt.strftime("%a %d %b · %H:%M %Z")
                        except Exception:
                            pub_str = pub
                    else:
                        pub_str = "—"
                    st.markdown(
                        f"<div style='background:#05966922;color:#059669;border-radius:4px;"
                        f"padding:4px 8px;font-size:12px;font-weight:700;text-align:center'>"
                        f"📢 Published {pub_str}</div>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**{post.get('title') or post.get('topic','Untitled')}**")
                    st.caption(f"{post.get('pillar','—')} · {post.get('platform','—')}")
                    if post.get("platform_post_id"):
                        st.caption(f"Post ID: `{post['platform_post_id']}`")

# --- Failed posts (sidebar alert) -------------------------------------------

if failed:
    st.divider()
    with st.expander(f"⚠️ {len(failed)} Failed Post(s)", expanded=False):
        for post in failed:
            st.error(
                f"**{post.get('title') or post.get('topic','Untitled')}** "
                f"({post.get('platform','—')})  \n"
                f"{post.get('error') or 'No error detail'}"
            )
