// MQTT-over-WebSocket client shim for the browser.
//
// Provides a thin native-JS wrapper around a WebSocket that speaks the
// MQTT 3.1.1 protocol to the broker (Paho on ws://host:9002/mqtt, or
// Adafruit IO on wss://io.adafruit.com/mqtt). Decodes PUBLISH payloads
// as UTF-8 JSON and hands them to the caller via `onEnvelope`.
//
// Features:
//   - Auto-reconnect with exponential backoff (1s → 2s → 4s → 8s → 16s
//     → 32s → 60s cap)
//   - Status events: connected | reconnecting | paused | error
//   - Long-disconnect tracking: after `longDisconnectMs` of disconnect
//     time, emits a `paused` event. The next successful connect after
//     a paused window carries `wasLongDisconnect: true`.
//   - No visibilitychange listener — the elapsed-time timer covers all
//     paths to a long disconnect.
//
// Exports `createMqttWsClient({ url, username, password, topic,
// longDisconnectMs, onEnvelope, onStatus })` as an ES module function.

const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 16000, 32000, 60000];

function utf8Encode(str) {
  if (typeof TextEncoder !== "undefined") {
    return new TextEncoder().encode(str);
  }
  const out = new Uint8Array(str.length);
  for (let i = 0; i < str.length; i++) out[i] = str.charCodeAt(i) & 0xff;
  return out;
}

function utf8Decode(bytes) {
  if (typeof TextDecoder !== "undefined") {
    return new TextDecoder("utf-8").decode(bytes);
  }
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return decodeURIComponent(escape(s));
}

// Encode a remaining length (variable-length) per MQTT 3.1.1 §2.2.3.
function encodeRemainingLength(len) {
  const bytes = [];
  do {
    let b = len % 128;
    len = Math.floor(len / 128);
    if (len > 0) b |= 0x80;
    bytes.push(b);
  } while (len > 0);
  return bytes;
}

function packet(type, variableHeader, payload) {
  // 1 byte fixed header (type << 4) + variable header + payload
  const vh = variableHeader || new Uint8Array(0);
  const pl = payload || new Uint8Array(0);
  const remainingLen = vh.length + pl.length;
  const header = new Uint8Array(1 + encodeRemainingLength(remainingLen).length);
  header[0] = (type << 4) & 0xff;
  const rlBytes = encodeRemainingLength(remainingLen);
  for (let i = 0; i < rlBytes.length; i++) header[1 + i] = rlBytes[i];
  const out = new Uint8Array(header.length + vh.length + pl.length);
  out.set(header, 0);
  out.set(vh, header.length);
  out.set(pl, header.length + vh.length);
  return out;
}

// Parse a UTF-8 string preceded by a 2-byte length (MQTT 3.1.1 §2.2.5).
function decodeString(bytes, offset) {
  const len = (bytes[offset] << 8) | bytes[offset + 1];
  const data = bytes.slice(offset + 2, offset + 2 + len);
  return { value: utf8Decode(data), next: offset + 2 + len };
}

function encodeString(str) {
  const data = utf8Encode(str);
  const out = new Uint8Array(2 + data.length);
  out[0] = (data.length >> 8) & 0xff;
  out[1] = data.length & 0xff;
  out.set(data, 2);
  return out;
}

function concat(...arrays) {
  let total = 0;
  for (const a of arrays) total += a.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const a of arrays) {
    out.set(a, off);
    off += a.length;
  }
  return out;
}

function buildConnect(username, password) {
  // Variable header: Protocol Name (MQTT), Protocol Level (4), Connect Flags,
  // Keep Alive (60).
  const protocolName = encodeString("MQTT");
  const protocolLevel = new Uint8Array([0x04]);
  const connectFlags = new Uint8Array([
    0x02 /* clean session */ |
      (username ? 0x80 : 0) |
      (password ? 0x40 : 0),
  ]);
  const keepAlive = new Uint8Array([0x00, 0x3c]); // 60 seconds
  const vh = concat(protocolName, protocolLevel, connectFlags, keepAlive);
  // Payload: client ID (required) + will topic/message (not used) + username + password
  const clientId = encodeString("lindsay-50-browser-" + Math.random().toString(16).slice(2, 10));
  let payload = clientId;
  if (username) payload = concat(payload, encodeString(username));
  if (password) payload = concat(payload, encodeString(password));
  return packet(1, vh, payload);
}

function buildSubscribe(topic) {
  // Variable header: Packet ID (0x0001), then topic filter + requested QoS (0).
  const vh = new Uint8Array([0x00, 0x01]);
  const filter = concat(encodeString(topic), new Uint8Array([0x00])); // QoS 0
  return packet(8, vh, filter);
}

function buildPingReq() {
  return packet(12);
}

// Parse a PUBLISH packet and return the payload as a string + the
// remaining bytes (so we can keep parsing in a streaming fashion).
function parsePublish(bytes) {
  // Fixed header byte 0: high nibble = packet type (3), low nibble = flags.
  const type = (bytes[0] >> 4) & 0x0f;
  if (type !== 3) return null;
  // Skip remaining length (assume 1 byte for our small broker packets).
  const len = bytes[1];
  let payloadStart = 2;
  if (len & 0x80) {
    // Multi-byte remaining length — not expected for our messages.
    return null;
  }
  // Variable header: Topic Name (2-byte length + string)
  const { value: topic, next: afterTopic } = decodeString(bytes, payloadStart);
  // Payload follows.
  const payloadBytes = bytes.slice(afterTopic, 2 + len);
  return { topic, payload: utf8Decode(payloadBytes) };
}

export function createMqttWsClient({
  url,
  username,
  password,
  topic,
  longDisconnectMs,
  onEnvelope,
  onStatus,
}) {
  const threshold = longDisconnectMs || 300000; // 5 minutes default
  let ws = null;
  let backoffIndex = 0;
  let reconnectTimer = null;
  let lastConnectedAt = null;
  let pauseTimer = null;
  let receivedConnAck = false;
  let buffer = new Uint8Array(0);
  let intentionalClose = false;

  function emitStatus(state, detail) {
    if (typeof onStatus === "function") {
      try {
        onStatus(state, detail || {});
      } catch (e) {
        console.error("onStatus callback error:", e);
      }
    }
  }

  function emitEnvelope(rawString) {
    if (typeof onEnvelope === "function") {
      try {
        onEnvelope(rawString);
      } catch (e) {
        console.error("onEnvelope callback error:", e);
      }
    }
  }

  function clearPauseTimer() {
    if (pauseTimer) {
      clearTimeout(pauseTimer);
      pauseTimer = null;
    }
  }

  function startPauseTimer() {
    clearPauseTimer();
    if (!lastConnectedAt) return;
    pauseTimer = setTimeout(() => {
      pauseTimer = null;
      const elapsedMs = Date.now() - lastConnectedAt;
      emitStatus("paused", { elapsedMs });
    }, threshold);
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    if (intentionalClose) return;
    const delay = RECONNECT_DELAYS_MS[Math.min(backoffIndex, RECONNECT_DELAYS_MS.length - 1)];
    backoffIndex += 1;
    emitStatus("reconnecting", { delayMs: delay, attempt: backoffIndex });
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function handleFrame(bytes) {
    if (bytes.length < 2) return;
    const type = (bytes[0] >> 4) & 0x0f;
    if (type === 2) {
      // CONNACK
      receivedConnAck = true;
    } else if (type === 3) {
      // PUBLISH
      const parsed = parsePublish(bytes);
      if (parsed) emitEnvelope(parsed.payload);
    } else if (type === 13) {
      // PINGRESP — no action
    }
  }

  function ingest(chunk) {
    // Concatenate chunk onto the buffer; try to parse out any full frames.
    const next = new Uint8Array(buffer.length + chunk.length);
    next.set(buffer, 0);
    next.set(chunk, buffer.length);
    buffer = next;
    while (buffer.length >= 2) {
      const len = buffer[1];
      if (len & 0x80) break; // multi-byte remaining length, wait
      const total = 2 + len;
      if (buffer.length < total) break;
      const frame = buffer.slice(0, total);
      buffer = buffer.slice(total);
      handleFrame(frame);
    }
  }

  function connect() {
    if (intentionalClose) return;
    clearPauseTimer();
    let socket;
    try {
      socket = new WebSocket(url, ["mqtt"]);
    } catch (e) {
      emitStatus("error", { error: String(e) });
      scheduleReconnect();
      return;
    }
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      try {
        socket.send(buildConnect(username, password));
        socket.send(buildSubscribe(topic));
        // No ping sent here; broker sends pingreq and we respond with pingresp.
        // (Sending our own ping keeps idle WS alive through corporate proxies
        // that close after ~60s; sent every 30s below.)
        setInterval(() => {
          try {
            if (socket.readyState === 1) socket.send(buildPingReq());
          } catch (e) {
            // ignore — close handler will trigger reconnect
          }
        }, 30000);
      } catch (e) {
        emitStatus("error", { error: String(e) });
        scheduleReconnect();
        return;
      }
      const wasLongDisconnect =
        lastConnectedAt !== null && Date.now() - lastConnectedAt >= threshold;
      lastConnectedAt = Date.now();
      backoffIndex = 0;
      const detail = { elapsedMs: wasLongDisconnect ? Date.now() - lastConnectedAt : 0 };
      if (wasLongDisconnect) detail.wasLongDisconnect = true;
      emitStatus("connected", detail);
      startPauseTimer();
    };
    socket.onmessage = (event) => {
      let data;
      if (event.data instanceof ArrayBuffer) {
        data = new Uint8Array(event.data);
      } else if (event.data instanceof Blob) {
        // Some browsers deliver Blob — convert via FileReader.
        const reader = new FileReader();
        reader.onload = () => ingest(new Uint8Array(reader.result));
        reader.readAsArrayBuffer(event.data);
        return;
      } else {
        // Text frame — treat as envelope payload directly.
        emitEnvelope(String(event.data));
        return;
      }
      ingest(data);
    };
    socket.onerror = (event) => {
      emitStatus("error", { error: "websocket error" });
    };
    socket.onclose = () => {
      // Disconnected — start the pause timer. If the elapsed time since
      // lastConnectedAt exceeds the threshold, the timer fires `paused`.
      startPauseTimer();
      scheduleReconnect();
    };
    ws = socket;
  }

  return {
    start() {
      intentionalClose = false;
      connect();
    },
    close() {
      intentionalClose = true;
      clearPauseTimer();
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (ws) {
        try {
          ws.close();
        } catch (e) {
          // ignore
        }
        ws = null;
      }
    },
  };
}
