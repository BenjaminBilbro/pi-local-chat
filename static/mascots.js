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

function applyMascots() {
  const active = readSet();
  const upcoming = nextMascotSet(active);

  document.documentElement.dataset.mascot = active;

  document.querySelectorAll('[data-mascot-img]').forEach((image) => {
    const state = image.dataset.mascotState || 'greeting';
    const src = state === 'landing' ? randomMascotSrc(active) : mascotSrc(state, active);
    if (image.getAttribute('src') !== src) image.setAttribute('src', src);
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

export function setupMascots() {
  applyMascots();

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
}
