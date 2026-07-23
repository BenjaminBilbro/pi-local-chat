const HEARTBEAT_INTERVAL_MS = 30_000;
const RECONNECT_DELAY_MS = 2_000;

export function createSocket({
  onOpen,
  onClose,
  onMessage,
  onUnauthorized,
}) {
  let connection = null;
  let heartbeatTimer = null;
  let reconnectTimer = null;
  let reconnectEnabled = true;

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

    socket.onopen = () => {
      clearInterval(heartbeatTimer);
      heartbeatTimer = setInterval(() => {
        send({ type: 'ping' });
      }, HEARTBEAT_INTERVAL_MS);
      onOpen();
    };

    socket.onmessage = (event) => {
      try {
        onMessage(JSON.parse(event.data));
      } catch (error) {
        console.error('Invalid WebSocket message', error);
      }
    };

    socket.onclose = (event) => {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
      if (connection === socket) connection = null;
      onClose(event);

      if (!reconnectEnabled) return;
      if (event.code === 4401) {
        onUnauthorized();
        return;
      }

      reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS);
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
