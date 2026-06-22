# Brite Tech Lifestyle — Brand Kit Reference

TRIGGER — load this skill BEFORE making any change that a user will see or that involves visual design. Do not rely on memory for colours, font weights, or button specs — always read from here. Auto-invoke whenever:
- editing `dashboard/app.py` (CSS, button styles, component layout, wording)
- editing `README.md` or any documentation
- updating the process diagram (`FLOW_HTML` in `dashboard/app.py`)
- changing infographic or carousel visual templates (`infographic_agent.py`, `carousel_agent.py`)
- writing UI labels, button text, status messages, section headings, or any copy the user reads
- someone asks what the brand colours, fonts, or design rules are

Apply these guidelines without being asked — never guess at a hex value or font weight.

---

## Identity

| Field | Value |
|-------|-------|
| Brand name | **Brite Tech Lifestyle** |
| Tagline | *"Technology, beautifully lived."* |
| Founder | Dean Britter |
| Voice | Clear, confident, warm. Never patronising. Short sentences. |

---

## Colour Tokens

| Token | Hex | Role |
|-------|-----|------|
| `--white` | `#FFFFFF` | Page / card background |
| `--off-white` | `#F5F5F7` | Section fills, sidebar, input bg |
| `--smoke` | `#E8E8ED` | Borders, dividers, subtle fills |
| `--silver` | `#A1A1A6` | Placeholder text, disabled states |
| `--slate` | `#6E6E73` | Body text, secondary labels |
| `--charcoal` | `#1D1D1F` | Primary text, headings, icons |
| `--black` | `#000000` | Logo, primary buttons, dark surfaces |
| `--accent` | `#0066CC` | Links, CTAs, eyebrow labels, accent fills |
| `--accent-lt` | `#E8F0FA` | Accent button background (light variant) |

**Rules:**
- White or off-white backgrounds only — never grey cards on grey pages.
- Accent blue (`#0066CC`) for links, active states, eyebrow labels — not for decorative colour blocks.
- Black (`#000000`) for the primary action button only.
- Never use off-brand colours (no amber, red, green for status — use charcoal/slate/accent).

---

## Typography

| Role | Family | Weight | Size | Tracking | Line-height |
|------|--------|--------|------|----------|-------------|
| Hero | Figtree | 800 | 72px | −0.045em | 1.0 |
| Title | Figtree | 700 | 44px | −0.03em | 1.1 |
| Heading | Figtree | 600 | 28px | −0.02em | — |
| Sub-heading / label | Figtree | 700 | 15–17px | −0.02em | — |
| Body | Figtree | 300 | 17px | — | 1.75 |
| UI / caption | Figtree | 400 | 13px | +0.01em | — |
| Eyebrow | Figtree | 600 | 11px | **+0.18em** | — |
| Tagline / quote | Playfair Display | italic | 22–24px | — | — |

**Rules:**
- Figtree for all UI, body, and headings. Playfair Display italic for taglines and pull quotes only — never for body text.
- Headings always negative tracking (−0.02em or tighter).
- Eyebrow labels: ALL CAPS, `+0.18em` tracking, accent colour, 11px/600.
- Body text: weight 300 (Light), not 400.

---

## Buttons

| Variant | Background | Text | Border | Hover |
|---------|-----------|------|--------|-------|
| Primary | `#000000` | `#FFFFFF` | none | `translateY(-2px)` |
| Accent | `#0066CC` | `#FFFFFF` | none | `translateY(-2px)` |
| Outline | transparent | `#1D1D1F` | 1.5px `#E8E8ED` | `translateY(-2px)` |
| Link | transparent | `#0066CC` | none | — |

- All buttons: `border-radius: 9999px` (full pill), `font-size: 14px`, `font-weight: 600`, `letter-spacing: 0.01em`, `padding: 12px 28px`.
- Every non-link button gets the `translateY(-2px)` lift on hover.
- The outline border is **1.5px** (not 1px).

---

## Component Patterns

**Cards / containers:**
- Background: `#FFFFFF`, border: `1px solid #E8E8ED`, border-radius: 16–18px, subtle shadow `0 1px 3px rgba(0,0,0,0.04)`.

**Expanders / panels:**
- Summary background: `#F5F5F7`, body background: `#FFFFFF`, border: `1px solid #E8E8ED`, border-radius: 14px.

**Status / eyebrow tags (pill chips):**
- Off-white background, 11px, 600 weight, 0.08–0.18em tracking, uppercase. Accent blue for active/info states.

**Dividers:** `1px solid #E8E8ED`. Never heavier.

---

## Voice & Copy Rules

- Short, punchy sentences. Never more than 15 words in a UI label.
- Confident but warm — avoid "please", "sorry", "unfortunately".
- Active voice. "Generate posts" not "Posts are generated".
- Status messages: plain English, no jargon. "Sent to Telegram" not "MANUAL_READY delivery".
- No exclamation marks in UI chrome (sparingly in success states only).
- Capitalise: brand name ("Brite Tech Lifestyle"), platform names ("Instagram", "LinkedIn"), proper nouns. Lowercase: feature names ("infographic reel", "competitor analysis", "daily research").

---

## Dashboard-specific Rules

- Tab labels: title case, concise (≤ 2 words).
- Section eyebrows: ALL CAPS, `+0.12em` tracking, accent blue.
- Button labels in sidebar controls: start with a verb ("Generate", "Publish", "Refresh").
- Badges: pill shape, small (10–11px), subdued colours (use status palette below).
- Error/warning/success states use the brand palette:
  - Success: `#1D7A34` text on `#1D7A340A` background
  - Warning: `#B25E09` text on `#B25E090A` background
  - Info: `#0066CC` text on `#E8F0FA` background
  - Error / failed: `#CC0000` text on `#FFF0F0` background (new — add to CSS if needed)
