# UI Refactor Task

Comprehensive guide for refactoring the pi-chat UI. This is the entry point for implementation — read this file first, then reference the linked docs as needed.

## Prerequisites

Before starting, read these files in order:

1. **[ARCHITECTURE.md](./ARCHITECTURE.md)** — How the app works, component responsibilities, rendering contract
2. **[TESTING.md](./TESTING.md)** — How to start the server, run camoufox-browser, and take screenshots
3. **[RPC_EVENT_FORMAT.md](./RPC_EVENT_FORMAT.md)** — Pi RPC event structures (reference only)

## Reference Screenshots

Real phone screenshots are saved in `screenshots/`:

| File | What it shows |
|------|---------------|
| `real-phone-navy-welcome.jpg` | Empty chat, navy theme, welcome text barely visible |
| `real-phone-navy-scroll.jpg` | Scrolled down, input bar below the fold, massive empty space |
| `real-phone-navy-chat.jpg` | Active chat with sub-agent cards, contrast issues in cards |
| `ui-improvement-ideas.md` | Full brainstorm from the exploration sub-agent |

---

## Observed Issues (from real phone screenshots)

### Critical

1. **Text contrast is unusable** — Navy theme: `#9ca2d1` background with `#FFF2F2` text has ~1.2:1 contrast ratio (WCAG AA requires 4.5:1). "Welcome" and "Start a conversation with pi" are barely readable. Blush theme: `#FFF2F2` background with `#9ca2d1` text has ~2.5:1 ratio.

2. **Input bar below the fold on mobile** — The welcome section has `padding: 60px 20px` and floats in the center of a 100vh flex container. On a phone (375px wide, ~667px visible), the user must scroll to find the input bar.

3. **Placeholder text invisible** — "Message pi..." uses `var(--text-dim)` which is nearly identical to the input background on navy theme.

### High Priority

4. **Sub-agent card text contrast** — Inside sub-agent cards, the tool call text (`bash ls -la /tmp`) and assistant messages use `--text-dim` on a navy background. The failed status badge (`#A43B4B` on navy) is hard to distinguish.

5. **Welcome section wastes space** — On mobile, the welcome message takes ~20% of the viewport but the input bar is pushed below the fold. The empty space between them serves no purpose.

6. **Header button hierarchy** — All header buttons (theme toggle, New Session, Sign out) have identical visual weight. "New Session" should be more prominent.

7. **Markdown code spans invisible** — Inline code uses `--code-bg: rgba(45, 51, 107, 0.1)` which is barely visible on either theme.

### Medium Priority

8. **Status dot too small** — 8px dot is easy to miss, especially the thinking state (yellow pulse).

9. **Scrollbar styling minimal** — 6px width with `--surface2` color is hard to notice.

10. **Session drawer text contrast** — Session preview text uses `--text` on `--surface` which inherits the contrast issues.

---

## Files to Modify

### Primary targets (CSS)

| File | What it controls |
|------|-----------------|
| `static/theme.css` | All color variables (`--navy`, `--blush`, `--text`, `--surface`, etc.) for both themes |
| `static/styles.css` | All layout, spacing, typography, component styling |

### Secondary targets (JS, if behavior changes needed)

| File | When to modify |
|------|---------------|
| `static/chat.js` | If welcome section needs dynamic behavior or input bar positioning changes |
| `static/history.js` | If historical rendering needs contrast-aware adjustments |
| `static/timeline.js` | If timeline primitives need style changes beyond CSS |
| `static/subagent.js` | If sub-agent card DOM structure needs changes |

---

## Implementation Steps

### Step 1: Fix color contrast (theme.css)

**File:** `static/theme.css`

The current palette has two themes: navy (dark background) and blush (light background). Both have critical contrast issues.

**Navy theme (current):**
```css
--navy: #9ca2d1;        /* Background — too light */
--blush: #FFF2F2;       /* Text — too light for navy bg */
```

**Proposed navy theme:**
```css
--navy: #2d336b;        /* Darker navy background */
--blush: #FFF2F2;       /* Keep as text — now readable */
--surface: #3d4382;     /* Darker surface for cards */
--surface2: #4a5090;    /* Darker borders */
--page-text-dim: #c8c0d0; /* Dimmed text with contrast */
--surface-text: #f0e8e8;  /* Light text on dark surfaces */
--surface-text-dim: #a89898; /* Dimmed surface text */
--code-bg: rgba(255, 242, 242, 0.12); /* Visible code bg */
--pre-bg: rgba(255, 242, 242, 0.16);  /* Visible pre bg */
```

**Proposed blush theme:**
```css
--bg: #FFF8F8;              /* Slightly warmer white */
--surface: #2d336b;         /* Navy surfaces for contrast */
--surface2: #3d4382;        /* Lighter navy borders */
--page-text: #2d336b;       /* Dark navy text */
--page-text-dim: #5d638e;   /* Dimmed navy text */
--surface-text: #FFF2F2;    /* Light text on navy surfaces */
--surface-text-dim: #c8c0d0; /* Dimmed light text */
--code-bg: rgba(45, 51, 107, 0.08); /* Visible code bg */
--pre-bg: rgba(45, 51, 107, 0.12);  /* Visible pre bg */
```

**Key principle:** Every text-to-background combination must pass WCAG AA (4.5:1 for normal text, 3:1 for large text). Test with the camoufox screenshots.

### Step 2: Fix mobile welcome section (styles.css)

**File:** `static/styles.css` — `.welcome` class and `@media (max-width: 600px)` block

Current `.welcome`:
```css
.welcome {
  text-align: center;
  padding: 60px 20px;
  color: var(--text-dim);
}
```

Changes:
- Reduce padding to `30px 20px` on mobile
- Consider adding `margin-top: auto` to push welcome to center only when there's no content, or use flexbox to keep input bar visible
- Alternative: Add `flex-shrink: 0` to `.input-area` and use `min-height` instead of `height: 100vh` on the messages container so the input bar is always visible

**Mobile media block changes:**
```css
@media (max-width: 600px) {
  .welcome {
    padding: 20px 16px;
    margin-top: 10vh;  /* Push down but not to center */
  }
  
  .welcome h2 {
    font-size: 18px;
  }
  
  .welcome p {
    font-size: 13px;
  }
  
  /* Ensure input area is always visible */
  .messages {
    padding: 12px;
    min-height: 0;  /* Allow flex to shrink */
  }
  
  .input-area {
    padding: 8px 12px;
  }
}
```

### Step 3: Fix input placeholder visibility (styles.css)

**File:** `static/styles.css` — `.input-wrapper textarea::placeholder`

Current:
```css
.input-wrapper textarea::placeholder {
  color: var(--text-dim);
  opacity: 0.8;
}
```

The `--text-dim` on navy theme is too light. Change to:
```css
.input-wrapper textarea::placeholder {
  color: var(--text-dim);
  opacity: 1;  /* Remove opacity reduction */
}
```

Or set a more explicit placeholder color in the mobile block:
```css
@media (max-width: 600px) {
  .input-wrapper textarea::placeholder {
    opacity: 1;
    color: var(--surface-text-dim);  /* Use surface text dim for contrast */
  }
}
```

### Step 4: Fix sub-agent card contrast (styles.css + theme.css)

**File:** `static/styles.css` — `.subagent-tool`, `.subagent-assistant-message`, `.subagent-tool-call`, `.subagent-summary`

Current sub-agent cards use `--text` and `--text-dim` which inherit the broken contrast. The cards also use `--surface` background which will be fixed in Step 1, but inner text needs explicit colors.

Changes in `styles.css`:
```css
.subagent-assistant-message {
  font-size: 14px;  /* Increase from 16px for mobile readability */
  color: var(--surface-text);  /* Explicit surface text color */
  line-height: 1.5;
}

.subagent-tool-call {
  color: var(--surface-text-dim);  /* Explicit dimmed text */
}

.subagent-tool-name {
  color: var(--surface-detail);  /* Explicit accent */
}

.subagent-summary {
  color: var(--surface-text);  /* Explicit text color */
}

.subagent-summary-status {
  /* Already has --success/--error colors, ensure they contrast with surface */
}
```

### Step 5: Improve header button hierarchy (styles.css)

**File:** `static/styles.css` — `.btn-icon`, `.header-actions`

Current all buttons use `.btn-icon` with identical styling. Differentiate:

```css
/* Primary action: New Session */
#new-session-btn {
  background: var(--accent);
  color: var(--accent-contrast);
  font-weight: 600;
}

#new-session-btn:hover {
  filter: brightness(1.1);
}

/* Secondary: Sign out */
#logout-btn {
  background: transparent;
  color: var(--text-dim);
  border: 1px solid var(--surface2);
}

/* Tertiary: Theme toggle (keep as-is) */
.btn-icon.theme-toggle {
  /* Existing styles */
}
```

### Step 6: Improve status dot (styles.css)

**File:** `static/styles.css` — `.status-dot`

Current:
```css
.status-dot {
  width: 8px;
  height: 8px;
}
```

Changes:
```css
.status-dot {
  width: 10px;
  height: 10px;
}

.status-dot.thinking {
  /* Add a more visible pulse */
}
```

### Step 7: Improve markdown code rendering (styles.css)

**File:** `static/styles.css` — `.markdown-content code`

Current:
```css
.markdown-content code {
  background: var(--code-bg);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 0.9em;
}
```

The `--code-bg` is too subtle. Update in `theme.css`:
```css
/* Navy theme */
--code-bg: rgba(255, 242, 242, 0.15);
--pre-bg: rgba(255, 242, 242, 0.18);

/* Blush theme */
--code-bg: rgba(45, 51, 107, 0.08);
--pre-bg: rgba(45, 51, 107, 0.12);
```

---

## Testing Checklist

After each step, verify:

1. **Start the server** (follow TESTING.md):
   ```bash
   cd /home/bbilbro/pi-chat
   PI_CHAT_TEST_HASH="$(uv run python -c "from pi_chat.auth import hash_password; print(hash_password('test-only'))")"
   PI_CHAT_DEV=1 PI_CHAT_B_PASSWORD_HASH="$PI_CHAT_TEST_HASH" uv run python server.py
   ```

2. **Run the render parity tests:**
   ```bash
   npm test
   ```
   This replays RPC captures through both live and historical renderers. If the assistant HTML doesn't match, the parity contract is broken.

3. **Take screenshots** (follow TESTING.md camoufox workflow) and verify:
   - [ ] Login screen is readable in both themes
   - [ ] Welcome text is clearly visible
   - [ ] Input bar is visible without scrolling on mobile
   - [ ] Placeholder text is readable
   - [ ] Sub-agent card text is readable
   - [ ] Failed status badges are visible
   - [ ] Markdown code spans are distinguishable
   - [ ] Session drawer text is readable

4. **Check contrast ratios** — Use the WebAIM contrast checker or browser dev tools to verify all text passes WCAG AA.

---

## Rendering Parity Contract

**Critical:** Live and historical rendering must produce identical DOM. The `ARCHITECTURE.md` "Rendering parity contract" section describes this. Any CSS change that affects both paths equally is safe. Any change that requires different DOM structure must be applied in both `static/chat.js` (live) and `static/history.js` (historical) using the shared primitives in `static/timeline.js` and `static/subagent.js`.

The `npm test` command enforces this automatically.

---

## Where to Look for Common Patterns

- **Color variables:** `static/theme.css` — all `--var` definitions
- **Component CSS:** `static/styles.css` — search for class names
- **Mobile overrides:** Bottom of `static/styles.css` — `@media (max-width: 600px)` block
- **Shared DOM creation:** `static/timeline.js` (thinking, tool, text, connectors), `static/subagent.js` (cards, snapshots)
- **Live rendering:** `static/chat.js` — `handlePiEvent()` and sub-functions
- **Historical rendering:** `static/history.js` — `renderHistoricalMessages()`, `renderAssistantRun()`

---

## Notes from Phone Screenshots

- The deployed instance is at `chat.pantingbean.org`
- Safari on iPhone shows the Safari toolbar (~70px) at the bottom, eating into viewport
- The `viewport` meta tag is set correctly (`width=device-width, initial-scale=1.0`)
- The mobile media query at 600px kicks in properly
- The header hamburger menu works but the drawer takes full width on mobile (correct behavior per current CSS)
- Sub-agent cards are functional but text contrast inside them needs improvement
- The timeline connectors render correctly on mobile
