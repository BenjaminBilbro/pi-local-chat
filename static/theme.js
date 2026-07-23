const STORAGE_KEY = 'pi-chat-theme';
const DEFAULT_THEME = 'blue';

export function setupTheme() {
  applyTheme(readStoredTheme());

  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const currentTheme = document.documentElement.dataset.theme;
      applyTheme(currentTheme === 'cream' ? 'blue' : 'cream');
    });
  });
}

function applyTheme(theme) {
  const activeTheme = theme === 'cream' ? 'cream' : 'blue';
  const nextTheme = activeTheme === 'blue' ? 'cream' : 'blue';
  document.documentElement.dataset.theme = activeTheme;

  try {
    localStorage.setItem(STORAGE_KEY, activeTheme);
  } catch {
    // The active theme still works when storage is unavailable.
  }

  document.querySelectorAll('[data-theme-toggle]').forEach((button) => {
    const label = button.querySelector('[data-theme-label]');
    const description = `${capitalize(nextTheme)} background`;
    if (label) label.textContent = description;
    button.title = `Switch to ${description.toLowerCase()}`;
    button.setAttribute(
      'aria-label',
      `Switch to ${description.toLowerCase()}`,
    );
  });
}

function readStoredTheme() {
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

function capitalize(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
