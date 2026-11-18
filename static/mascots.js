/* Two mascot sets, switchable. Persisted in localStorage ('pi-chat-mascot').
 *
 *   pie  the pi pie: /static/pi-mascot-<state>.svg
 *   pi   the person in the pi shirt: /static/mascots/pi-<state>.svg
 *
 * Everything that shows a mascot carries data-mascot-img, and the state it wants in
 * data-mascot-state ('greeting' by default, or 'landing' for the randomized landing avatar).
 * Toggling re-points every one of them without a reload.
 */

const STORAGE_KEY = 'pi-chat-mascot';
const DEFAULT_SET = 'pie';

const SETS = {
  pie: { label: 'Pie', prefix: '/static/pi-mascot-' },
  pi: { label: 'Pi', prefix: '/static/mascots/pi-' },
};

const STATES = ['greeting', 'searching', 'success', 'helpful'];

function readSet() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && SETS[stored]) return stored;
  } catch {
    // Storage unavailable (private mode): the default set still renders.
  }
  return DEFAULT_SET;
}

export function currentMascotSet() {
  return readSet();
}

export function nextMascotSet(from = readSet()) {
  return from === 'pie' ? 'pi' : 'pie';
}

export function mascotSrc(state = 'greeting', set = readSet()) {
  const chosen = STATES.includes(state) ? state : 'greeting';
  return `${SETS[set].prefix}${chosen}.svg`;
}

export function randomMascotSrc(set = readSet()) {
  return mascotSrc(STATES[Math.floor(Math.random() * STATES.length)], set);
}

const HEADER_VISIBILITY_KEY = 'pi-chat-header-mascot';

function readHeaderVisible() {
  try {
    return localStorage.getItem(HEADER_VISIBILITY_KEY) !== 'hidden';
  } catch {
    return true;
  }
}

function applyMascots() {
  const active = readSet();
  const upcoming = nextMascotSet(active);

  document.documentElement.dataset.mascot = active;

  document.querySelectorAll('[data-mascot-img]').forEach((image) => {
    const state = image.dataset.mascotState || 'greeting';
    const src = state === 'landing' ? randomMascotSrc(active) : mascotSrc(state, active);
    if (image.getAttribute('src') !== src) image.setAttribute('src', src);
  });

  // The Settings dialog picks the set with two chips rather than a header button that cycled between them.
  // A cycle hides how many options exist and gives no way to name the one you want; `aria-pressed` on each
  // chip says which one is live, which is the same information the old preview image was trying to carry.
  document.querySelectorAll('[data-mascot-set]').forEach((chip) => {
    const on = chip.dataset.mascotSet === active;
    chip.setAttribute('aria-pressed', String(on));
    chip.classList.toggle('active', on);
  });

  document.querySelectorAll('[data-mascot-toggle]').forEach((button) => {
    const preview = button.querySelector('.mascot-toggle-preview');
    const label = button.querySelector('[data-mascot-label]');
    if (preview) preview.src = mascotSrc('greeting', upcoming);
    if (label) label.textContent = SETS[upcoming].label;
    const description = `Switch mascot to ${SETS[upcoming].label}`;
    button.title = description;
    button.setAttribute('aria-label', description);
  });
}

/** Hide the pie beside the chat title, for someone who wants a plainer bar. Scoped by CSS to the chat
 *  header only: the login screen's mascot stays, because that one is the thing you are signing into. */
function applyHeaderMascotVisibility() {
  const visible = readHeaderVisible();
  document.body?.classList.toggle('hide-header-mascot', !visible);
  document.querySelectorAll('[data-header-mascot-toggle]').forEach((button) => {
    button.setAttribute('aria-pressed', String(visible));
    button.textContent = visible ? 'Shown' : 'Hidden';
  });
}

export function setupMascots() {
  applyMascots();
  applyHeaderMascotVisibility();

  document.querySelectorAll('[data-mascot-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      try {
        localStorage.setItem(STORAGE_KEY, nextMascotSet());
      } catch {
        // Still switch for this page view even when storage refuses.
      }
      applyMascots();
    });
  });

  document.querySelectorAll('[data-mascot-set]').forEach((chip) => {
    chip.addEventListener('click', () => {
      try {
        localStorage.setItem(STORAGE_KEY, chip.dataset.mascotSet);
      } catch {
        // Still switch for this page view even when storage refuses.
      }
      applyMascots();
    });
  });

  document.querySelectorAll('[data-header-mascot-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      try {
        localStorage.setItem(HEADER_VISIBILITY_KEY, readHeaderVisible() ? 'hidden' : 'shown');
      } catch {
        // Storage refused: the class below still changes it for this page view.
      }
      applyHeaderMascotVisibility();
    });
  });
}
