# Sunday design tokens

Everything visual is a CSS custom property in `sunday/web/static/sunday.css`. New screens use these; nothing is hard-coded. Light values below; dark mode swaps the same names under `prefers-color-scheme: dark`.

## Colour

| Token | Light | Use |
|---|---|---|
| `--paper` | #f7f2ea | page background |
| `--paper-2` | #efe8dc | secondary surfaces, pills, empty squares |
| `--card` | #fffcf7 | cards |
| `--ink` | #1f1c18 | headings, primary text |
| `--ink-2` | #514a41 | body text (7.8:1 on paper) |
| `--ink-3` | #6a6257 | captions, labels (5.4:1 on paper; the smallest text allowed) |
| `--line` / `--line-2` | #e6ddcf / #d4c9b7 | borders, inputs |
| `--accent` / `--accent-ink` / `--accent-soft` | #c2553a / #8f3a24 / #f6e4dd | buttons, links, highlights |
| `--ok` / `--ok-ink` / `--bright` | #6c8e67 / #3e6a3e / #3f7f4e | okay, good, bright moods |
| `--low` / `--warn-ink` / `--today` | #c08a2a / #7a5309 / #c4861a | a bit low, check today |
| `--now` | #b2372a | urgent |
| `--info` | #7d879b | informational |

All text colours meet WCAG AA (4.5:1) on `--paper` and `--card`. Mood is never shown by colour alone: squares and legends carry a letter (B, O, L, !).

## Type

- Display: **Fraunces** (variable, 400 to 600, optical sizing on). `h1` uses `opsz 144`; `h2`/`h3` use `opsz 24`.
- Text: **Inter** (variable, 400 to 700).
- Both self-hosted under `/static/fonts/`, Latin subsets, SIL Open Font License.
- Minimum size: `--fs-min` = 14px. `.tiny`, labels, captions and strip letters all use it.
- Scale: body 16px/1.6; h1 `clamp(2rem, 4vw, 2.8rem)`; `.greet h1` up to 3rem; hero up to 3.9rem.

## Shape and depth

- Radii: `--r-lg` 20px (cards, tiles), `--r` 14px, `--r-sm` 10px (inputs, alerts), 999px pills and buttons.
- Shadows: `--shadow` (resting cards), `--shadow-lg` (hover, device frame, toast).
- Borders: 1px `--line`; alerts carry a 4px left border in their tier colour.

## Components

`.btn` (+ `.ghost`, `.quiet`, `.sm`, `.lg`, `.icon`), `.card` (+ `.tight`), `.tile` (+ `.t-*` tints, `.ok/.warn/.bad`), `.banner` (urgent takeover), `.alert`, `.chip`, `.pill`, `.av` (initials avatar, six hues from a name hash), `.strip` (fortnight with `data-tip` tooltips), `.stat`, `.list`, `.day` (timeline card with `<details>`), `.turns` + `mark` (transcript with evidence), `.seg` (segmented control), `.switch`, `.phone-in` (country code + number), `.progress` + `.wstep` (wizard), `.steps-live` (call status), `.appnav` (tabs; bottom bar under 640px), `.crumbs`, `.toast`.

## Motion

Toast in/out, WhatsApp bubbles arriving in the hero (`wa-in`), the urgent banner pulsing once (`pulse-once`), the live ring on the call page (`ring`), tiles lifting on hover. Everything is disabled under `prefers-reduced-motion`.

## Icons

Inline SVG, 24-unit grid, 1.8 stroke, round caps and joins (Lucide-style). No emoji or Unicode glyphs for UI.
