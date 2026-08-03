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
   hardcode a page ID — it can change.
2. **Use the Session Log database** the HQ page links to — don't create a
   separate "Dev Log". If HQ has no Session Log at all, create one with the
   fields below and say so in the chat reply. Read the HQ's "How Claude works
   with this workspace" child page first if it exists — it's the canonical,
   cross-surface version of this rule and takes precedence over this skill if
   the two ever disagree.
3. **Add one entry per session**, with these fields:

| Field | Contents |
|-------|----------|
| Entry | One line, plain English, what *happened* — not what was discussed |
| Date | Session date |
| Type | `Fix` / `Deliverable` / `Rule change` / `Decision` / `Research` / `Measurement` / `Automation run` |
| Workstream | Which Brite area(s) this belongs to (e.g. Reels & Social, Etsy & Pinterest, Ops & Admin) |
| Surface | `Claude Code`, `Cowork chat`, `Claude app`, `Scheduled task`, or `Design` |
| What changed | Specifics — files, commit SHAs, resolved worker version, numbers |
| Decisions & reasoning | **Highest-value field.** The option chosen, options rejected, and why |
| Follow-ups | Anything only Dean can do (env var, token re-auth, approval), or work still open. Empty if nothing |
| Verified | Tick only if you actually checked the outcome afterwards, and say how in "What changed" |

## Rules that matter

- **Plain language, not commit-speak.** The entry must make sense to Dean later
  without reading the diff. "LinkedIn posts now also go to the business page via
  Telegram, because the API needs an approval we can't get" — not "refactor
  `_publish_linkedin` to multi-target dispatch".
- **Decisions & reasoning is what a future session can't reconstruct** from the
  diff alone — record what was rejected and why, not just what shipped.
- **Follow-ups are the second highest-value field.** A new env var that never
  gets set means the feature silently does nothing in production. Always
  surface: variables to set, tokens to re-authorise, APIs to approve.
- **Never** write secrets, tokens, API keys, passwords, or personal details into
  Notion. Name the variable (`LINKEDIN_ORG_URN`), never its value.
- **Honesty over completeness.** If the Notion MCP tools aren't connected, say so
  in the chat reply and give Dean the entry text to paste manually. Never skip it
  silently, and never claim something was logged when it wasn't.
- **Don't backfill history that predates this rule.** Only sessions from when
  the logging rule took effect need an entry — don't invent retroactive entries
  for older, already-shipped commits.
- **Don't restructure his workspace.** Add entries; don't reorganise, rename, or
  delete existing Notion content without asking.
