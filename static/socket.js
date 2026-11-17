const HEARTBEAT_INTERVAL_MS = 30_000;

/* Reconnect backoff. A flat 2 s used to mean a flapping tailnet route was retried flat out forever,
 * and every landed attempt is a fresh WebSocket plus a session round trip on the server. Delays grow
 * to a ceiling with jitter, and attempts are never capped: an hour-later reconnect after a laptop
 * sleep is legitimate and must still happen by itself. Two tabs that dropped on the same tunnel blip
 * must not come back in lockstep, which is what the jitter is for.
 */
const RECONNECT_MIN_DELAY_MS = 1_000;
const RECONNECT_MAX_DELAY_MS = 30_000;

export function createSocket({
  onOpen,
  onClose,
  onMessage,
  onBinary,
  onUnauthorized,
  onHandlerError,
}) {
  let connection = null;
  let heartbeatTimer = null;
  let reconnectTimer = null;
  let reconnectEnabled = true;
  let reconnectAttempt = 0;

  function scheduleReconnect() {
    const ceiling = Math.min(
      RECONNECT_MAX_DELAY_MS,
      RECONNECT_MIN_DELAY_MS * 2 ** reconnectAttempt,
    );
    reconnectAttempt += 1;
    // Half the interval of jitter, so the first retry is 500 ms to 1 s and a long outage settles
    // somewhere in the last 15 s to 30 s rather than at a fixed beat.
    const delay = Math.round(ceiling / 2 + Math.random() * (ceiling / 2));
    reconnectTimer = setTimeout(connect, delay);
    return delay;
  }

  function connect() {
    if (
      connection
      && (
        connection.readyState === WebSocket.OPEN
        || connection.readyState === WebSocket.CONNECTING
      )
    ) {
      return;
    }

    clearTimeout(reconnectTimer);
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const sessionPath = new URLSearchParams(location.search).get('session');
    const url = sessionPath
      ? `${protocol}://${location.host}/ws?session=${encodeURIComponent(sessionPath)}`
      : `${protocol}://${location.host}/ws`;

    const socket = new WebSocket(url);
    connection = socket;
    window.__piSocket = socket;  // Expose for STT audio streaming
    socket.binaryType = 'arraybuffer';

    socket.onopen = () => {
      reconnectAttempt = 0; // a landed connection is what resets the backoff
      clearInterval(heartbeatTimer);
      heartbeatTimer = setInterval(() => {
        send({ type: 'ping' });
      }, HEARTBEAT_INTERVAL_MS);
      onOpen();
    };

    socket.onmessage = (event) => {
      if (typeof event.data !== 'string') {
        onBinary?.(event.data);
        return;
      }
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        console.error('Invalid WebSocket message', error);
        return;
      }
      /* The handler is deliberately outside that try. A few weeks ago a render exception while loading
       * a session was reported as "Invalid WebSocket message" and swallowed, so the user saw an empty
       * conversation with no clue that the app had thrown (2026-11-09, the `docPane` ReferenceError).
       * Bad payload is the server's problem; a throw in our own dispatch is ours, and gets said out loud.
       */
      try {
        onMessage(message);
      } catch (error) {
        console.error(`Failed to handle ${message?.type ?? 'message'}`, error);
        onHandlerError?.({ type: message?.type, error });
      }
    };

    socket.onclose = (event) => {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
      if (connection === socket) connection = null;
      if (window.__piSocket === socket) window.__piSocket = null;
      onClose(event);

      if (!reconnectEnabled) return;
      if (event.code === 4401) {
        onUnauthorized();
        return;
      }

      scheduleReconnect();
    };
  }

  function send(message) {
    if (!connection || connection.readyState !== WebSocket.OPEN) {
      return false;
    }
    connection.send(JSON.stringify(message));
    return true;
  }

  function disconnect() {
    reconnectEnabled = false;
    clearTimeout(reconnectTimer);
    clearInterval(heartbeatTimer);
    reconnectTimer = null;
    heartbeatTimer = null;
    connection?.close();
    connection = null;
  }

  return { connect, disconnect, send };
}
