const STORAGE_KEY_SHORTCUT_HINT = 'pi-chat-shortcut-hint';
const HINT_DELAY_MS = 5_000;

const commands = [
  {
    id: 'new-session',
    label: 'New Session',
    shortcut: 'Ctrl+Shift+N',
    action: () => window.__paletteActions?.newSession(),
  },
  {
    id: 'open-sessions',
    label: 'Open Sessions',
    shortcut: 'Ctrl+O',
    action: () => window.__paletteActions?.openSessions(),
  },
  {
    id: 'toggle-theme',
    label: 'Toggle Theme',
    shortcut: 'Ctrl+Shift+T',
    action: () => window.__paletteActions?.toggleTheme(),
  },
  {
    id: 'focus-composer',
    label: 'Focus Composer',
    shortcut: 'Ctrl+L',
    action: () => window.__paletteActions?.focusComposer(),
  },
  {
    id: 'sign-out',
    label: 'Sign Out',
    shortcut: null,
    action: () => window.__paletteActions?.signOut(),
  },
];

let dialogElement = null;
let listElement = null;
let searchInput = null;
let isOpen = false;
let selectedIndex = -1;
let filteredCommands = [...commands];

export function setupPalette() {
  dialogElement = document.getElementById('command-palette-dialog');
  listElement = document.getElementById('command-palette-list');
  searchInput = document.getElementById('command-palette-search');

  if (!dialogElement || !listElement || !searchInput) return;

  dialogElement.addEventListener('click', handleBackdropClick);
  searchInput.addEventListener('input', handleSearch);

  document.addEventListener('keydown', handleGlobalKeyDown);

  showHint();
}

function showHint() {
  try {
    if (localStorage.getItem(STORAGE_KEY_SHORTCUT_HINT) === 'dismissed') return;
  } catch {
    return;
  }

  setTimeout(() => {
    if (isOpen) return;
    const hint = document.getElementById('shortcut-hint');
    if (!hint) return;

    hint.classList.add('active');
    const dismiss = hint.querySelector('#shortcut-hint-dismiss');
    dismiss?.addEventListener('click', () => {
      hint.classList.remove('active');
      try { localStorage.setItem(STORAGE_KEY_SHORTCUT_HINT, 'dismissed'); } catch {}
    });

    setTimeout(() => hint.classList.remove('active'), 4000);
  }, HINT_DELAY_MS);
}

function handleGlobalKeyDown(event) {
  // Always handle Escape to close
  if (event.key === 'Escape' && isOpen) {
    closePalette();
    return;
  }

  // When palette is open, handle navigation
  if (isOpen) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      navigate(1);
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      navigate(-1);
      return;
    }
    if (event.key === 'Enter' && selectedIndex >= 0) {
      event.preventDefault();
      executeCommand(selectedIndex);
      return;
    }
    return;
  }

  // Global shortcuts when palette is closed
  const isMac = navigator.platform.toUpperCase().includes('MAC');
  const modifier = isMac ? event.metaKey : event.ctrlKey;

  // Ctrl+K / Cmd+K — open palette
  if (modifier && event.key.toLowerCase() === 'k' && !event.shiftKey) {
    event.preventDefault();
    openPalette();
    return;
  }

  // Ctrl+Shift+N — new session
  if (modifier && event.shiftKey && event.key.toLowerCase() === 'n') {
    event.preventDefault();
    commands.find((c) => c.id === 'new-session')?.action();
    return;
  }

  // Ctrl+O — open sessions
  if (modifier && !event.shiftKey && event.key.toLowerCase() === 'o') {
    event.preventDefault();
    commands.find((c) => c.id === 'open-sessions')?.action();
    return;
  }

  // Ctrl+Shift+T — toggle theme
  if (modifier && event.shiftKey && event.key.toLowerCase() === 't') {
    event.preventDefault();
    commands.find((c) => c.id === 'toggle-theme')?.action();
    return;
  }

  // Ctrl+L — focus composer
  if (modifier && !event.shiftKey && event.key.toLowerCase() === 'l') {
    event.preventDefault();
    commands.find((c) => c.id === 'focus-composer')?.action();
    return;
  }
}

function handleBackdropClick(event) {
  if (event.target === dialogElement) closePalette();
}

function handleSearch(event) {
  const query = event.target.value.toLowerCase().trim();
  filteredCommands = query
    ? commands.filter((c) => c.label.toLowerCase().includes(query))
    : [...commands];
  selectedIndex = -1;
  renderList();
}

function openPalette() {
  isOpen = true;
  dialogElement.classList.add('active');
  dialogElement.setAttribute('aria-hidden', 'false');
  filteredCommands = [...commands];
  selectedIndex = -1;
  searchInput.value = '';
  renderList();
  searchInput.focus();
}

function closePalette() {
  isOpen = false;
  dialogElement.classList.remove('active');
  dialogElement.setAttribute('aria-hidden', 'true');
  searchInput.blur();
  searchInput.value = '';
  filteredCommands = [...commands];
  selectedIndex = -1;
}

function renderList() {
  listElement.innerHTML = '';

  if (filteredCommands.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'palette-empty';
    empty.textContent = 'No commands found';
    listElement.appendChild(empty);
    return;
  }

  filteredCommands.forEach((command, index) => {
    const item = document.createElement('button');
    item.className = 'palette-item';
    item.dataset.index = index;

    if (index === selectedIndex) item.classList.add('selected');

    const left = document.createElement('span');
    left.className = 'palette-item-label';
    left.textContent = command.label;

    item.appendChild(left);

    if (command.shortcut) {
      const right = document.createElement('span');
      right.className = 'palette-item-shortcut';
      right.textContent = command.shortcut;
      item.appendChild(right);
    }

    item.addEventListener('click', () => executeCommand(index));
    item.addEventListener('mouseenter', () => {
      selectedIndex = index;
      updateSelection();
    });

    listElement.appendChild(item);
  });
}

function updateSelection() {
  const items = listElement.querySelectorAll('.palette-item');
  items.forEach((item, index) => {
    item.classList.toggle('selected', index === selectedIndex);
  });

  const selected = items[selectedIndex];
  selected?.scrollIntoView({ block: 'nearest' });
}

function navigate(direction) {
  if (filteredCommands.length === 0) return;

  if (selectedIndex < 0 && direction < 0) {
    selectedIndex = filteredCommands.length - 1;
  } else {
    selectedIndex += direction;
  }

  if (selectedIndex < 0) selectedIndex = filteredCommands.length - 1;
  if (selectedIndex >= filteredCommands.length) selectedIndex = 0;

  updateSelection();
}

function executeCommand(index) {
  const command = filteredCommands[index];
  if (!command) return;

  closePalette();
  command.action();
}
