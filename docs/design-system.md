# Design context

The styling decisions this app is built on, recorded 2026-11-08 before the restyle and kept current by hand.
`AGENTS.md` says the UI is visually frozen; this file is what "frozen" means in concrete terms, so a change
can respect the design without someone having to reverse-engineer it from `styles.css`.

The palette and type below are **locked**. They are not a default to revisit when a framework shows up; they
are the reason the app looks like one thing instead of everything.

## Surface

Local web app: FastAPI serving a static vanilla-JS frontend (`static/`, no framework, no build step). One
`pi --mode rpc` subprocess per browser connection. Styled by `static/theme.css` (tokens only) plus
`static/styles.css` (layout and components). There is no build pipeline, so design work means editing those
two files directly, and there is no minifier standing between a typo and the browser.

## Audience

Household, not customer-facing: two people, from a desktop and from a phone over Tailscale. It is nonetheless
branded deliberately rather than generically, and it doubles as a running demonstration of the aesthetic.

## Theme

Both, with a toggle in the header persisted in `localStorage` under `pi-chat-theme`, default **light**. Light
is the Oat/Light surface with Navy text; dark is the Navy ramp. The earlier theme identifiers were `navy` and
`blush`; `theme.js` maps any stored old value to the current `light` / `dark` keys so nobody's saved
preference breaks on an upgrade.

## Use case

Chat with a pi coding-agent session from anywhere on the tailnet, watch streaming answers and tool and
sub-agent activity as they happen, and reopen past sessions.

## Palette and type (locked)

- Palette: Lava 600 `#FF3621` used as a 10% accent, Navy 900 `#0B2026`, Light `#F9F7F4`, Oat neutrals.
- Typography: DM Sans 400/500/700 plus italic 400, DM Mono 400/500. No substitutes, and no other families.
- Components: no component library, so the register is carried by the patterns themselves: button, input,
  dialog, badge and list item all behave the same way everywhere, and a new control copies an existing one
  rather than inventing a fifth shape.
- Voice: editorial-plain. Sentence case, no superlatives, imperative verbs on actions.

## Constraints

- **Zero third-party requests at runtime.** No Google Fonts, no CDNs. DM Sans and DM Mono are vendored as
  latin woff2 under `static/fonts/`. The app is used on a tailnet where the internet may be down, and
  household chats should not generate outbound telemetry.
- **Phone-first responsive.** Tailnet access means a 390 to 430 px viewport is a first-class target, with
  44 px touch targets on anything tappable.
- **WCAG 2.1 AA** on every text and background pair, in both themes. Lava 600 never carries body text on a
  light surface.
- **Reduced motion** respected via `prefers-reduced-motion`.
- **Buddy's art stays as drawn.** The existing SVG avatar and pie art are Ben's; the styling covers the
  chrome around the character, not the character.
- Legacy token names (`--navy`, `--blush`, `--surface`, `--page-*`, `--surface-*`, `--radius`, `--pre-bg`,
  the bubble tokens) still appear throughout `styles.css`. They stay defined in `theme.css` as aliases so the
  layout layer keeps working; new CSS uses the current names.

## Testing what this file claims

`npm test` runs `tests/compare_render.py`, which is about rendering parity rather than color. The design
constraints above are checked by eye plus `node tools/screenshot_ui.mjs` at both desktop and 390 px widths
against the fixture server (`docs/dev-workflow.md`). There is no automated contrast or font check, and
claiming otherwise would be the kind of lie this file exists to prevent.
