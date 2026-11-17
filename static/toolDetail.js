/**
 * The detail view for one tool call, and the only place its controls are wired.
 *
 * A row in the timeline is a summary; this is what you open when a call is worth looking at - what it
 * was pointed at, how long it has been going, what came back. It is also the one place a reader can
 * stop a run: pi has no per-tool cancel (its only lever is `abort`, which kills the tool's process
 * tree *and* ends the run), so the copy here says that out loud instead of implying a precision the
 * protocol does not have.
 *
 * Nothing in it is rendered as HTML. Arguments and results are model and tool output, which is
 * untrusted input, and every field is written with textContent.
 */

const STATE_LABELS = {
  pending: 'Streaming arguments',
  running: 'Running',
  ok: 'Completed',
  error: 'Failed',
  cancelled: 'Cancelled',
  unknown: 'No result recorded',
};

const OPEN_STATES = new Set(['pending', 'running']);

/* A 20-minute bash run can return megabytes, and a textarea-sized pre is not a reader's friend. The
 * head and tail together are what you actually scan for "why did that fail". */
const MAX_SHOWN = 12_000;

const CANCEL_NOTE = 'Cancelling stops the whole run, not just this call: pi kills the tool and ends '
  + 'the turn, and anything already written to disk stays written. Queued messages are pulled out of '
  + 'the queue and handed back to the composer.';

function formatElapsed(record, now) {
  const from = record.executionStartedAt || record.startedAt;
  if (!from) return '-';

  const seconds = Math.max(0, Math.round(((record.finishedAt || now) - from) / 1000));
  if (seconds < 60) return `${seconds}s${record.finishedAt ? '' : ' so far'}`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s`;
}

function clampBody(value) {
  const text = String(value ?? '');
  if (!text) return '';
  if (text.length <= MAX_SHOWN) return text;

  const head = text.slice(0, MAX_SHOWN * 0.7);
  const tail = text.slice(-MAX_SHOWN * 0.3);
  return `${head}\n\n[... ${(text.length - MAX_SHOWN).toLocaleString()} characters omitted ...]\n\n${tail}`;
}

function argsBody(record) {
  const args = record?.args;
  if (args && typeof args === 'object' && Object.keys(args).length > 0) {
    try {
      return clampBody(JSON.stringify(args, null, 2));
    } catch {
      return clampBody(String(args));
    }
  }

  /* The arguments are still arriving. There is nothing parsed to show, and the raw text is the only
   * thing that says what a long `write` is pointed at while it streams - the dialog redraws once a
   * second, so a file body grows here in front of the reader instead of appearing all at once. */
  if (!record?.argsComplete && record?.partialArgs) {
    return `${clampBody(record.partialArgs)}

[arguments still arriving]`;
  }

  return '';
}

export function createToolDetail({ sendCommand }) {
  const overlay = document.getElementById('tool-detail-overlay');
  const dialog = document.getElementById('tool-detail-dialog');
  const title = dialog?.querySelector('#tool-detail-title');
  const stateValue = dialog?.querySelector('[data-tool-detail-state]');
  const elapsedValue = dialog?.querySelector('[data-tool-detail-elapsed]');
  const argsValue = dialog?.querySelector('[data-tool-detail-args]');
  const resultBlock = dialog?.querySelector('[data-tool-detail-result-block]');
  const resultValue = dialog?.querySelector('[data-tool-detail-result]');
  const note = dialog?.querySelector('[data-tool-detail-note]');
  const cancelButton = dialog?.querySelector('[data-tool-detail-cancel]');
  const closeButton = dialog?.querySelector('[data-tool-detail-close]');

  let record = null;
  let opener = null;
  let openId = '';
  let cancelSent = false;
  let ticker = null;

  function render() {
    if (!record) return;

    const status = record.element?.dataset.toolStatus || record.status || 'unknown';
    stateValue.textContent = STATE_LABELS[status] || status;
    elapsedValue.textContent = formatElapsed(record, Date.now());

    const args = argsBody(record);
    argsValue.textContent = args || 'No arguments recorded for this call.';

    const result = clampBody(record.resultText || '');
    resultValue.textContent = result || (OPEN_STATES.has(status)
      ? 'Nothing has come back yet.'
      : 'No result was recorded for this call.');
    resultBlock.hidden = false;

    const cancellable = OPEN_STATES.has(status);
    cancelButton.hidden = !cancellable;
    note.hidden = !cancellable;
    if (cancellable) {
      note.textContent = CANCEL_NOTE;
      cancelButton.disabled = cancelSent;
      cancelButton.textContent = cancelSent ? 'Stopping the run' : 'Cancel and stop the run';
    }
  }

  function stopTicking() {
    if (ticker) clearInterval(ticker);
    ticker = null;
  }

  function startTicking() {
    stopTicking();
    // One timer for both jobs: a running call's elapsed count, and noticing that the cancel landed.
    ticker = setInterval(render, 1000);
  }

  function open(nextRecord, element) {
    if (!overlay || !nextRecord) return;

    record = nextRecord;
    opener = element || document.activeElement;
    openId = nextRecord.element?.dataset?.toolCallId || '';
    cancelSent = false;

    title.textContent = nextRecord.name || 'Tool call';
    argsValue.textContent = '';
    render();
    startTicking();

    overlay.hidden = false;
    document.body.classList.add('tool-detail-open');
    closeButton?.focus();
  }

  function close() {
    if (!overlay || overlay.hidden) return;

    stopTicking();
    overlay.hidden = true;
    document.body.classList.remove('tool-detail-open');
    record = null;

    /* Settlement rebuilds the timeline, so the row this was opened from may no longer be in the
     * document. Follow the call id rather than dropping focus on the floor. */
    const target = opener?.isConnected
      ? opener
      : (openId ? document.querySelector(`[data-tool-call-id="${openId}"] .tool-line`) : null);
    if (target?.focus) target.focus();
    opener = null;
    openId = '';
  }

  overlay?.addEventListener('click', (event) => {
    if (event.target === overlay) close();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !overlay?.hidden) {
      event.stopPropagation();
      close();
    }
  });

  closeButton?.addEventListener('click', close);

  cancelButton?.addEventListener('click', () => {
    if (cancelSent) return;

    cancelSent = true;
    render();
    // The server does clear_queue then abort: pi cannot cancel one call without ending the run, and
    // a queued message that has not been delivered should come back rather than run into a dead run.
    sendCommand({ type: 'cancel_tool' });
  });

  return {
    open,
    close,
    // The header Stop button and this dialog can both start a cancel; keep the dialog honest when
    // the reader used the header instead.
    noteCancelSent() {
      cancelSent = true;
      render();
    },
    isOpen() {
      return Boolean(overlay && !overlay.hidden);
    },
  };
}
