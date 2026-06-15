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

function packet(type, flags, variableHeader, payload) {
  // 1 byte fixed header (type << 4 | flags) + variable header + payload
  const vh = variableHeader || new Uint8Array(0);
  const pl = payload || new Uint8Array(0);
  const remainingLen = vh.length + pl.length;
  const header = new Uint8Array(1 + encodeRemainingLength(remainingLen).length);
  header[0] = ((type << 4) | (flags & 0x0f)) & 0xff;
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
  return packet(1, 0, vh, payload);
}

function buildSubscribe(topic) {
  // Variable header: Packet ID (0x0001), then topic filter + requested QoS (0).
  const vh = new Uint8Array([0x00, 0x01]);
  const filter = concat(encodeString(topic), new Uint8Array([0x00])); // QoS 0
  return packet(8, 0x02, vh, filter);
}

function buildPingReq() {
  return packet(12, 0);
}

// Decode MQTT 3.1.1 §2.2.3 remaining length (variable-length, up to 4 bytes).
// Returns { value, bytesUsed } where bytesUsed is the total bytes consumed
// (1 fixed header + N length bytes). Returns null if malformed.
function decodeRemainingLength(buf) {
  let multiplier = 1;
  let value = 0;
  let bytesUsed = 0;
  for (let i = 1; i < Math.min(5, buf.length); i++) {
    const b = buf[i];
    value += (b & 0x7f) * multiplier;
    bytesUsed += 1;
    if ((b & 0x80) === 0) {
      return { value, bytesUsed: bytesUsed + 1 };
    }
    multiplier *= 128;
  }
  return null;
}

// Parse a PUBLISH packet and return the payload as a string + the
// remaining bytes (so we can keep parsing in a streaming fashion).
function parsePublish(bytes) {
  // Fixed header byte 0: high nibble = packet type (3), low nibble = flags.
  const type = (bytes[0] >> 4) & 0x0f;
  if (type !== 3) return null;
  // Skip the multi-byte remaining length. We don't need the value here
  // — the bytes we want to parse (topic + payload) start right after
  // the remaining-length field and run to the end of the buffer.
  const decoded = decodeRemainingLength(bytes);
  if (!decoded) return null;
  let payloadStart = decoded.bytesUsed;
  // Variable header: Topic Name (2-byte length + string)
  const { value: topic, next: afterTopic } = decodeString(bytes, payloadStart);
  // Payload follows to end of frame.
  const payloadBytes = bytes.slice(afterTopic);
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
  let pingInterval = null;
  let backoffIndex = 0;
  let reconnectTimer = null;
  let lastConnectedAt = null;
  // Timestamp of the FIRST close in the current disconnect cycle.
  // Drives the pause timer: "paused" is emitted only when the connection
  // has been actually disconnected for >= threshold ms. Set on the first
  // onclose of a cycle and cleared on the next successful onopen — not
  // reset by intermediate close events during the reconnect loop, so
  // sustained network outages still trigger "paused" even when the WS
  // cycles through close → connect → close repeatedly.
  let disconnectedSince = null;
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

  // Schedule a "paused" emit at `disconnectedSince + threshold`. The
  // timer is anchored to the FIRST close in the cycle, not to the
  // most recent close — so a tight reconnect loop (close → connect →
  // close, every 1s) still fires "paused" once the total disconnect
  // duration reaches threshold. If we're already past the threshold
  // when this is called, fire immediately rather than scheduling
  // (otherwise repeated calls would create a queue of pending timers).
  function startPauseTimer() {
    clearPauseTimer();
    if (disconnectedSince === null) return;
    const elapsed = Date.now() - disconnectedSince;
    if (elapsed >= threshold) {
      emitStatus("paused", { elapsedMs: elapsed });
      return;
    }
    pauseTimer = setTimeout(() => {
      pauseTimer = null;
      emitStatus("paused", { elapsedMs: Date.now() - disconnectedSince });
    }, threshold - elapsed);
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
      const returnCode = bytes[3] || 0;
      if (returnCode !== 0) {
        // Non-zero = connection refused. Surface a useful error so the
        // MQTT status flips to "Error" and the admin UI sees why.
        const reasons = {
          1: "protocol version",
          2: "client identifier rejected",
          3: "server unavailable",
          4: "bad credentials",
          5: "not authorized",
        };
        console.warn(
          "[mqtt-ws] CONNACK refused:",
          reasons[returnCode] || `code ${returnCode}`
        );
        emitStatus("error", { error: reasons[returnCode] || `code ${returnCode}` });
      }
    } else if (type === 3) {
      // PUBLISH
      let parsed = null;
      try {
        parsed = parsePublish(bytes);
      } catch (e) {
        console.error("[mqtt-ws] PUBLISH parse threw:", e && e.message, e);
      }
      if (parsed) {
        emitEnvelope(parsed.payload);
      } else {
        console.warn(
          "[mqtt-ws] PUBLISH parse failed, bytes=",
          Array.from(bytes.slice(0, 20))
            .map((b) => b.toString(16).padStart(2, "0"))
            .join(" ")
        );
      }
    } else if (type === 9) {
      // SUBACK — no-op. Granted QoS is in bytes[3]; we don't need to act
      // on it because QoS 0 is fine for our local UI and the broker
      // reports success (0x00) for both granted-QoS-0 and granted-QoS-1.
    } else if (type === 13) {
      // PINGRESP — no action
    } else if (type !== 0) {
      // type 0 is reserved and shouldn't appear on the wire.
      console.info(
        "[mqtt-ws] unknown frame type:",
        type,
        "bytes:",
        Array.from(bytes.slice(0, 20))
          .map((b) => b.toString(16).padStart(2, "0"))
          .join(" ")
      );
    }
  }

  function decodeRemainingLength(buf) {
    // MQTT 3.1.1 §2.2.3: each byte carries 7 bits of the length; the
    // high bit (0x80) signals "more bytes follow". Up to 4 bytes for
    // 268 MB payloads, but practically 2 covers anything we'd see.
    let multiplier = 1;
    let value = 0;
    let bytesUsed = 0;
    for (let i = 1; i < Math.min(5, buf.length); i++) {
      const b = buf[i];
      value += (b & 0x7f) * multiplier;
      bytesUsed += 1;
      if ((b & 0x80) === 0) {
        return { value, bytesUsed: bytesUsed + 1 /* include the fixed header byte */ };
      }
      multiplier *= 128;
    }
    return null; // malformed — 5th continuation bit would overflow
  }

  function ingest(chunk) {
    // Concatenate chunk onto the buffer; try to parse out any full frames.
    const next = new Uint8Array(buffer.length + chunk.length);
    next.set(buffer, 0);
    next.set(chunk, buffer.length);
    buffer = next;
    let safety = 0;
    while (buffer.length >= 2 && safety++ < 10) {
      const decoded = decodeRemainingLength(buffer);
      if (!decoded) {
        // Need more bytes to finish the remaining-length field, or it's
        // malformed (>4 bytes, which the broker never produces).
        if (buffer.length >= 5) {
          console.warn("[mqtt-ws] malformed remaining length, dropping");
          buffer = new Uint8Array(0);
        }
        break;
      }
      const total = decoded.bytesUsed + decoded.value;
      if (buffer.length < total) break; // partial frame, wait for more
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
        if (pingInterval) clearInterval(pingInterval);
        pingInterval = setInterval(() => {
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
      // We're back — clear the disconnect marker so the next onclose
      // will start a fresh pause window. Do NOT call startPauseTimer
      // here: the pause is reserved for "the connection has been
      // actually disconnected for >= threshold", not "we've been
      // connected for a long time". The previous version scheduled
      // a timer on every open, which flipped the status to "Paused"
      // 5 minutes after each successful reconnect — even when the
      // WS was still perfectly healthy.
      disconnectedSince = null;
      clearPauseTimer();
      const detail = { elapsedMs: wasLongDisconnect ? Date.now() - lastConnectedAt : 0 };
      if (wasLongDisconnect) detail.wasLongDisconnect = true;
      emitStatus("connected", detail);
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
      // The WebSocket `error` event doesn't carry detail in browsers, but
      // the immediately-following `close` event will (code 1006 means the
      // socket was closed without a close frame — typically the broker
      // dropped the TCP connection or the WS upgrade response was rejected).
      console.warn("[mqtt-ws] ws error event fired");
      emitStatus("error", { error: "websocket error" });
    };
    socket.onclose = (event) => {
      // Stop the ping interval on the dead socket — a new one is set up
      // on the next successful onopen.
      if (pingInterval) {
        clearInterval(pingInterval);
        pingInterval = null;
      }
      // Disconnected — log the close code/reason to help diagnose
      // browser-specific failures (e.g. corporate proxies that close
      // the WS without sending a 101, or Chrome's stricter subprotocol
      // negotiation on localhost).
      console.warn(
        "[mqtt-ws] ws close:",
        event.code,
        event.reason,
        "wasClean=" + event.wasClean
      );
      // Mark the start of the disconnect cycle. If we're already in a
      // cycle (e.g. the reconnect loop is cycling close → connect →
      // close), `disconnectedSince` is left as-is so the pause timer
      // keeps counting from the FIRST close, not the most recent one.
      if (disconnectedSince === null) {
        disconnectedSince = Date.now();
      }
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
