const STORAGE_KEY = 'pi-chat-theme';
const DEFAULT_THEME = 'light';
const META_COLORS = {
  light: '#F9F7F4',
  dark: '#0B2026',
};

// Themes were called 'blush' (light) and 'navy' (dark) before the 2026-11-08 rebrand.
const LEGACY_THEMES = {
  blush: 'light',
  navy: 'dark',
};

export function setupTheme() {
  applyTheme(readStoredTheme());

  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      applyTheme(nextTheme(document.documentElement.dataset.theme));
    });
  });
}

function normalize(theme) {
  if (theme === 'light' || theme === 'dark') return theme;
  return LEGACY_THEMES[theme] || DEFAULT_THEME;
}

function nextTheme(theme) {
  return normalize(theme) === 'dark' ? 'light' : 'dark';
}

function applyTheme(theme) {
  const activeTheme = normalize(theme);
  const upcoming = nextTheme(activeTheme);

  document.documentElement.dataset.theme = activeTheme;
  document.documentElement.style.colorScheme = activeTheme;

  const metaThemeColor = document.querySelector('meta[name="theme-color"]');
  if (metaThemeColor) {
    metaThemeColor.content = META_COLORS[activeTheme];
  }

  try {
    localStorage.setItem(STORAGE_KEY, activeTheme);
  } catch {
    // The active theme still works when storage is unavailable.
  }

  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    const label = button.querySelector('[data-theme-label]');
    const description = `Switch to ${upcoming} theme`;
    // The button used to read "Theme" because the icon beside it carried the state. In the Settings row the
    // word "Theme" is already on the left, so repeating it there said nothing; the switch now names the
    // theme you are in, and the aria-label keeps naming the one the click will give you.
    if (label) label.textContent = activeTheme === 'dark' ? 'Dark' : 'Light';
    button.title = description;
    button.setAttribute('aria-label', description);
  });
}

function readStoredTheme() {
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}
