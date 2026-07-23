export function setupAuth(onAuthenticated) {
  const loginScreen = document.getElementById('login-screen');
  const chatScreen = document.getElementById('chat-screen');
  const passphraseInput = document.getElementById('passphrase');
  const loginButton = document.getElementById('login-btn');
  const loginError = document.getElementById('login-error');
  const passwordArea = document.getElementById('login-password-area');
  const accountButtons = {
    b: document.getElementById('icon-b'),
    r: document.getElementById('icon-r'),
  };

  let selectedAccount = null;

  function completeLogin(account) {
    selectedAccount = account;
    loginScreen.classList.add('hidden');
    chatScreen.classList.add('active');
    onAuthenticated();
  }

  function selectAccount(account) {
    selectedAccount = account;
    loginError.textContent = '';
    passphraseInput.value = '';

    for (const [key, button] of Object.entries(accountButtons)) {
      button.className = `login-icon${key === account ? ` selected-${key}` : ''}`;
    }

    passwordArea.classList.add('active');
    passphraseInput.focus();
  }

  async function attemptLogin() {
    if (!selectedAccount) return;

    loginButton.disabled = true;
    loginError.textContent = '';
    try {
      const response = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          account: selectedAccount,
          password: passphraseInput.value,
        }),
      });

      if (response.ok) {
        const session = await response.json();
        passphraseInput.value = '';
        completeLogin(session.account);
        return;
      }

      loginError.textContent = 'Incorrect password';
    } catch {
      loginError.textContent = 'Unable to reach the server';
    } finally {
      loginButton.disabled = false;
    }

    passphraseInput.value = '';
    passphraseInput.focus();
  }

  async function restoreSession() {
    try {
      const response = await fetch('/api/me');
      if (!response.ok) return;
      const session = await response.json();
      completeLogin(session.account);
    } catch {
      // The regular login screen remains available if the server is offline.
    }
  }

  accountButtons.b.addEventListener('click', () => selectAccount('b'));
  accountButtons.r.addEventListener('click', () => selectAccount('r'));
  loginButton.addEventListener('click', attemptLogin);
  passphraseInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') attemptLogin();
  });

  restoreSession();
}
