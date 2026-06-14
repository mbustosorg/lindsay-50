// Browser-side IndexedDB persistence for the message ring buffer and
// the SignConfig. Called from the base template's `app.js` (plain JS)
// and from PyScript via `create_proxy` for the `/preview` page's
// on_message wiring.
//
// Schema:
//   db: lindsay-50-browser (configurable via dbName)
//     object store: messages
//       keyPath: id (the message UUID)
//       index:   by-received_at (received_at, descending)
//     object store: config
//       keyPath: key
//       records: { key: "current", value: <SignConfig dict> }
//
// All write operations return { ok: true } on success or
// { ok: false, error: <string> } on failure — they NEVER throw across
// the JS bridge into Python. A write failure (quota, private mode,
// IndexedDB unavailable) is a non-fatal warning.
//
// Exports `createMessageBufferStore({ dbName })` as an ES module.

const MAX_MESSAGES = 100;

function openDb(dbName) {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("IndexedDB unavailable in this runtime"));
      return;
    }
    const req = indexedDB.open(dbName, 1);
    req.onupgradeneeded = (event) => {
      const db = event.target.result;
      if (!db.objectStoreNames.contains("messages")) {
        const store = db.createObjectStore("messages", { keyPath: "id" });
        store.createIndex("by-received_at", "received_at");
      }
      if (!db.objectStoreNames.contains("config")) {
        db.createObjectStore("config", { keyPath: "key" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("IndexedDB open failed"));
  });
}

function withStore(db, storeName, mode, fn) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const store = tx.objectStore(storeName);
    let result;
    try {
      result = fn(store, tx);
    } catch (e) {
      reject(e);
      return;
    }
    tx.oncomplete = () => resolve(result);
    tx.onerror = () => reject(tx.error || new Error("IndexedDB transaction failed"));
    tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted"));
  });
}

function readAll(store) {
  return new Promise((resolve, reject) => {
    const req = store.getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

export function createMessageBufferStore({ dbName }) {
  if (!dbName) dbName = "lindsay-50-browser";
  let dbPromise = null;

  function getDb() {
    if (!dbPromise) dbPromise = openDb(dbName);
    return dbPromise;
  }

  async function hydrate() {
    let db;
    try {
      db = await getDb();
    } catch (e) {
      return { messages: [], config: null };
    }
    try {
      const messages = await withStore(db, "messages", "readonly", (store) =>
        readAll(store)
      );
      // Sort by received_at descending
      messages.sort((a, b) => (a.received_at > b.received_at ? -1 : 1));
      // Take the most recent MAX_MESSAGES
      const trimmed = messages.slice(0, MAX_MESSAGES);
      const configRows = await withStore(db, "config", "readonly", (store) =>
        readAll(store)
      );
      const configRow = configRows.find((r) => r.key === "current") || null;
      return { messages: trimmed, config: configRow ? configRow.value : null };
    } catch (e) {
      return { messages: [], config: null };
    }
  }

  async function wipe() {
    let db;
    try {
      db = await getDb();
    } catch (e) {
      return { ok: false, error: String(e) };
    }
    try {
      await withStore(db, "messages", "readwrite", (store) => {
        store.clear();
      });
      await withStore(db, "config", "readwrite", (store) => {
        store.clear();
      });
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  async function putMessage(msg) {
    if (!msg || !msg.id) {
      return { ok: false, error: "message missing id" };
    }
    let db;
    try {
      db = await getDb();
    } catch (e) {
      return { ok: false, error: String(e) };
    }
    try {
      await withStore(db, "messages", "readwrite", (store, tx) => {
        store.put(msg);
        // Atomic trim: if total > MAX_MESSAGES, drop the oldest by received_at.
        const countReq = store.count();
        countReq.onsuccess = () => {
          if (countReq.result > MAX_MESSAGES) {
            const cursorReq = store.index("by-received_at").openCursor();
            const toDelete = countReq.result - MAX_MESSAGES;
            let deleted = 0;
            cursorReq.onsuccess = (event) => {
              const cursor = event.target.result;
              if (cursor && deleted < toDelete) {
                cursor.delete();
                deleted += 1;
                cursor.continue();
              }
            };
          }
        };
      });
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  async function putConfig(dict) {
    let db;
    try {
      db = await getDb();
    } catch (e) {
      return { ok: false, error: String(e) };
    }
    try {
      await withStore(db, "config", "readwrite", (store) => {
        store.put({ key: "current", value: dict });
      });
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  return {
    hydrate,
    wipe,
    putMessage,
    putConfig,
  };
}
