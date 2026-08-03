# Claude Code Notes — social-media-bot

## Session logging → Notion (Brite Tech Lifestyle HQ)

**Every session that changes code must log itself to the Brite Tech Lifestyle HQ
in Notion before finishing.** Dean uses that page — not the chat history — as the
record of what the system does and why, so a change that isn't logged there is
effectively invisible.

**Find the page** with `notion-search` for "Brite Tech Lifestyle HQ" (don't
hardcode a page ID — it can change). Look for a "Dev Log" section/database; if
there isn't one, create a `Dev Log` database on the HQ page with the properties
below and say so in chat.

**Log one entry per session**, with:

| Field | Contents |
|-------|----------|
| Date | Session date |
| Summary | What changed and, more importantly, **why** — the problem it fixes |
| Areas | Files/agents touched (e.g. `publisher_agent.py`, LinkedIn) |
| Commit | Short SHA(s) pushed to `main` |
| Worker version | The `_WORKER_VERSION` after the change |
| Action needed | Anything only Dean can do — set a Railway env var, re-authorise a token, approve an API. **Empty if nothing.** |

**Rules**

- Write it in **plain language**, not commit-speak. The entry should make sense to
  Dean in three months without reading the diff.
- **Action items are the highest-value part.** A new env var that never gets set
  means the feature silently does nothing in production — always surface those.
- **Skip** sessions that changed no code (questions, explanations, investigations
  that concluded "no change needed").
- **One entry per session**, not per commit. Amend the entry if more work follows
  in the same session.
- **Never** put secrets, tokens, API keys, or personal details in Notion.
- If the Notion MCP tools aren't connected, **say so in the chat reply** and give
  Dean the entry text to paste — don't skip it silently and don't claim it was
  logged.

## Git workflow

Commit changes **directly to `main`**. Do **not** open feature branches or pull
requests unless explicitly asked — the maintainer prefers to avoid manual merges.

In sandboxed Claude Code web sessions a normal `git push` is blocked by the
egress proxy, so commit via the GitHub API tools (`create_or_update_file` /
`push_files`) — these write straight to `main`. After committing, run
`git pull origin main` to bring the local sandbox in sync.

## Screen Corner Calibration

Corners for each scene are stored in `core/cover_image.py` → `_SCENE_CORNERS` as
`(is_white_screen, [TL, TR, BR, BL])` in original image pixel coordinates (portrait
1744×2336 for scenes 1/2/4, square 2048×2048 for scene 3).

### Calibration render

Generate a semi-transparent coloured card (RGBA alpha=210) with a bright yellow border
(width=5) and a 6×5 grid of faint lines, plus TL/TR/BL/BR corner labels. Warp it onto
the scene using `_perspective_coeffs` + Pillow PERSPECTIVE transform, then square-crop
centred on the screen corners and resize to 1080×1080. Send old vs new side-by-side so
the user can compare alignment at each corner independently. The grid is essential — it
lets the user see which edge is out and by how much.

```python
# Core warp
A, b = [], []
for (sx, sy), (dx, dy) in zip(card_corners, screen_corners):
    A.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
    A.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
    b.extend([sx, sy])
coeffs = tuple(float(c) for c in np.linalg.solve(np.array(A), np.array(b)))
warped = card.transform((sw, sh), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
mask = Image.new("L", (scr_w, scr_h), 255).transform(
    (sw, sh), Image.PERSPECTIVE, coeffs, Image.NEAREST
)
comp = scene.copy()
comp.paste(warped.convert("RGBA"), (0, 0), mask)
```

### Pixel edge analysis

| Screen type | Threshold |
|-------------|-----------|
| Black screen (scenes 1/2/4) | brightness < 30 |
| White screen (scene 3)       | brightness > 220 |

- **Top edge** — scan each column, find first run of ≥3 consecutive dark/bright pixels
- **Left edge** — find bezel peak (`np.argmax` in narrow x-band), then first dark/bright
  pixel after it; the bezel is the bright metallic reflection just outside the screen glass
- **Right edge** — last dark/bright column per row
- **Bottom edge** — last dark/bright row per column

### Key lessons

- **Left edge slope matters** — the screen's left edge often leans *right* as y increases
  (perspective). Getting the slope wrong causes TL to look fine but BL to overshoot, or
  vice versa. Measure the slope explicitly: `(BL.x - TL.x) / (BL.y - TL.y)`.
- **Iterate one corner at a time** once the others are confirmed good.
- **Scale of feedback**: "a matter of pixels" ≈ 3–8 px; "tiny bit" ≈ 8–15 px;
  "a little" ≈ 15–25 px; "completely out" ≈ 50+ px.
- When the user says a side is good, lock those corners and only adjust the others.
- After confirming a scene, commit immediately with the old/new values in the message.
