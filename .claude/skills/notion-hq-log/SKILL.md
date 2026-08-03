---
name: notion-hq-log
description: Log a Claude Code work session to the Brite Tech Lifestyle HQ page in Notion. Use this at the END of ANY session that changed code, shipped a feature, fixed a bug, or produced a deliverable for Brite Tech Lifestyle — automatically, without being asked. Also use when the user says "log this", "log to Notion", "update the HQ", "update Notion", "add this to the dev log", or asks what was done in a past session. Applies to every Brite project — the social media automation bot, Etsy listings, Pinterest pins, skills work — not just one repo.
---

# Log the session to Brite Tech Lifestyle HQ (Notion)

Dean uses the **Brite Tech Lifestyle HQ** page in Notion — not the chat history —
as the durable record of what his systems do and why. Chat sessions disappear;
that page is what he actually reads in three months. **A change that isn't logged
there is effectively invisible.**

## When to run

- **Automatically**, at the end of any session that changed code or produced a
  deliverable. Don't wait to be asked.
- **Skip** sessions that changed nothing — questions, explanations, and
  investigations that concluded "no change needed". Don't log noise.
- **One entry per session**, not per commit. If more work follows in the same
  session, update the existing entry rather than adding a second.

## How to write the entry

1. **Find the page.** `notion-search` for "Brite Tech Lifestyle HQ". Do **not**
   hardcode a page ID — it can change. Look for a "Dev Log" database or section.
2. **If no Dev Log exists**, create a `Dev Log` database on the HQ page using the
   fields below, and say so in the chat reply.
3. **Add the entry:**

| Field | Contents |
|-------|----------|
| Date | Session date |
| Project | Which Brite system (e.g. Social Media Bot, Etsy, Pinterest) |
| Summary | What changed and, more importantly, **why** — the problem it solves |
| Areas | Files/agents/areas touched (e.g. `publisher_agent.py`, LinkedIn) |
| Commit | Short SHA(s) pushed |
| Version | Resulting `_WORKER_VERSION`, where the project has one |
| Action needed | Anything only Dean can do. **Leave empty if nothing.** |

## Rules that matter

- **Plain language, not commit-speak.** The entry must make sense to Dean later
  without reading the diff. "LinkedIn posts now also go to the business page via
  Telegram, because the API needs an approval we can't get" — not "refactor
  `_publish_linkedin` to multi-target dispatch".
- **Action items are the highest-value field.** A new env var that never gets set
  means the feature silently does nothing in production. Always surface: Railway
  variables to set, tokens to re-authorise, APIs to approve, things to verify.
- **Never** write secrets, tokens, API keys, passwords, or personal details into
  Notion. Name the variable (`LINKEDIN_ORG_URN`), never its value.
- **Honesty over completeness.** If the Notion MCP tools aren't connected, say so
  in the chat reply and give Dean the entry text to paste manually. Never skip it
  silently, and never claim something was logged when it wasn't.
- **Don't restructure his workspace.** Add entries; don't reorganise, rename, or
  delete existing Notion content without asking.
