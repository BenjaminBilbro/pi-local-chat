/* Hot reload: notice that the code on disk has moved, apply it, and land back where you were.

   The loop this is trying to shorten is the one where a change is edited, then thought about, then applied by
   a refresh that may or may not have been needed, then verified by hunting for the thing you changed. Two
   facts about this app decide the shape:

   - front-end files are read from disk per request and sent with `Cache-Control: no-cache`, so a refresh
     already applies them. What no one can see is *that* a refresh is owed.
   - Python is read once at import, so a refresh only reconnects to the process holding the old code. Only a
     new process applies it, which in dev mode the server can be asked for (`POST /api/dev/restart`).

   So there are two banners and they do different things: one reloads the page, the other asks the supervisor
   for a new process and reloads when it answers. Both restore the conversation, because losing your place is
   the reason a person postpones the reload and then debugs against code they edited an hour ago.
*/

const POLL_MS = 4000;
/* Survives a reload and dies with the tab. `localStorage` would restore a conversation days later, which is
   a different feature with a different name and would surprise anyone who opens the app fresh. */
const RESTORE_KEY = 'pi-chat-dev-reload';

export function createDevReload({ currentSessionPath, doc = document, win = window } = {}) {
  let baseline = null;
  let timer = null;
  let busy = false;
  let banner = null;
  /* Set while the banner is asking something. Without it the next poll, four seconds later, decides "nothing
   * changed since I last looked" and hides the prompt nobody answered yet: the banner would blink once and
   * the person would have to catch it mid-blink. */
  let pending = false;

  function endpoint() {
    return `${win.location.origin}/api/version`;
  }

  async function version() {
    const response = await fetch(endpoint(), { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error(`version ${response.status}`);
    return response.json();
  }

  function ensureBanner() {
    if (banner) return banner;
    banner = doc.createElement('div');
    banner.id = 'dev-reload';
    banner.setAttribute('role', 'status');
    banner.hidden = true;
    doc.body.appendChild(banner);
    return banner;
  }

  /** @param {{label: string, action?: {kind: 'reload'|'restart', text: string}}} state */
  function show({ label, action }) {
    const node = ensureBanner();
    node.innerHTML = '';

    const text = doc.createElement('span');
    text.className = 'dev-reload-label';
    text.textContent = label;
    node.append(text);

    if (action) {
      const button = doc.createElement('button');
      button.type = 'button';
      button.className = 'dev-reload-action';
      button.textContent = action.text;
      button.disabled = busy;
      button.addEventListener('click', () => {
        pending = false;
        return action.kind === 'restart' ? restart() : reloadNow();
      });
      node.append(button);
    }

    const dismiss = doc.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'dev-reload-dismiss';
    dismiss.setAttribute('aria-label', 'Ignore until the next change');
    dismiss.textContent = '\u00d7';
    dismiss.addEventListener('click', () => {
      // Suppressed against this exact stamp pair, not turned off: the next edit earns the banner back.
      if (baseline) baseline = { ...baseline, dismissedFrontend: baseline.frontend, dismissedBackend: baseline.backend };
      pending = false;
      node.hidden = true;
    });
    node.append(dismiss);
    node.hidden = false;
    pending = true;
  }

  function hide() {
    pending = false;
    if (banner) banner.hidden = true;
  }

  /** Remember where the person is, then leave the page. Called from both paths. */
  function reloadNow() {
    try {
      const path = typeof currentSessionPath === 'function' ? currentSessionPath() : '';
      win.sessionStorage.setItem(RESTORE_KEY, JSON.stringify({ path: path || '', at: Date.now() }));
    } catch {
      // A storage exception costs the restore and nothing else. The reload is still the right action, and
      // throwing here would leave the person on a page whose banner button does nothing.
    }
    win.location.reload();
  }

  async function restart() {
    if (busy) return;
    busy = true;
    const bootWas = baseline ? baseline.bootId : null;
    show({ label: 'restarting the server…' });
    try {
      const response = await fetch(`${win.location.origin}/api/dev/restart`, {
        method: 'POST',
        credentials: 'same-origin',
      });
      if (!response.ok) throw new Error(`restart ${response.status}`);
    } catch (error) {
      busy = false;
      show({ label: `restart failed: ${error.message}. Run server.py yourself, or use the terminal.` });
      return;
    }

    /* The old process is gone before the new one listens, so failures while polling are the expected middle
       of this, not an error to report. What is being waited for is a *different* boot id: a stamp alone would
       be satisfied by the dying process answering one more time. */
    const deadline = Date.now() + 45000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      try {
        const next = await version();
        if (next.bootId && next.bootId !== bootWas) {
          reloadNow();
          return;
        }
      } catch {
        // Still down, or still coming up.
      }
    }
    busy = false;
    show({ label: 'no server answered in 45s. Check the terminal: a restart that crashes is the one thing the banner cannot fix.' });
  }

  async function tick() {
    if (busy || pending) return;
    let snapshot;
    try {
      snapshot = await version();
    } catch {
      // A failed poll is a server that is not in dev mode, not logged in, or restarting. Saying nothing is
      // correct: this module is a developer convenience and must never become a banner the family sees.
      hide();
      return;
    }

    if (!baseline) {
      baseline = { ...snapshot };
      return;
    }

    const frontendChanged = snapshot.frontend && snapshot.frontend.stamp !== baseline.frontend?.stamp;
    const backendChanged = snapshot.backend && snapshot.backend.stamp !== baseline.backend?.stamp;

    if (frontendChanged && snapshot.frontend.stamp !== baseline.dismissedFrontend) {
      show({
        label: `${snapshot.frontend.newest || 'front-end'} changed`,
        action: { kind: 'reload', text: 'Reload' },
      });
      baseline = { ...baseline, frontend: snapshot.frontend.stamp };
      return;
    }
    if (backendChanged && snapshot.backend.stamp !== baseline.dismissedBackend) {
      if (snapshot.restartable) {
        show({
          label: `${snapshot.backend.newest || 'back-end'} changed`,
          action: { kind: 'restart', text: 'Restart server' },
        });
      } else {
        // Not running under the dev supervisor: say what is true rather than offering a button that cannot work.
        show({ label: `${snapshot.backend.newest || 'back-end'} changed. Restart server.py to apply it.` });
      }
      baseline = { ...baseline, backend: snapshot.backend.stamp };
      return;
    }
    hide();
  }

  timer = win.setInterval(tick, POLL_MS);
  doc.addEventListener('visibilitychange', () => {
    if (!doc.hidden) tick();
  });
  tick();

  return {
    probe: () => ({ baseline, busy }),
    stop: () => win.clearInterval(timer),
    tick,
    /** Consumed once, by app.js, the first time the socket opens after a reload. */
    takePendingRestore() {
      try {
        const raw = win.sessionStorage.getItem(RESTORE_KEY);
        if (!raw) return '';
        win.sessionStorage.removeItem(RESTORE_KEY);
        const parsed = JSON.parse(raw);
        // Two answers, and both matter: a restore that is this old describes a conversation the person has
        // probably left, and an empty path means the reload happened on a brand new session, which is nothing
        // to reopen.
        if (!parsed.path || Date.now() - (parsed.at || 0) > 60000) return '';
        return parsed.path;
      } catch {
        return '';
      }
    },
  };
}
