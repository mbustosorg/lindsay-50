// Dashboard modals + test injection (issue #48, §6 + round-5 changes
// 2026-07-23).
//
// Three diagnostic modals on the dashboard header:
//
//   1. Current config — reads the live SignConfig via
//      `App.getConfig()` and renders the full `to_dict()` shape
//      as pretty-printed JSON. The round-5 implementation uses
//      the same `to_dict()` → `Array.from(keys)` → `.get(key)`
//      walk that `dashboard_recent.js` uses for messages, so the
//      modal survives PyProxy enumeration being non-standard
//      (`Object.keys(dictProxy)` returns `[]` for Python dicts).
//
//   2. Suppression reasons — round-5 simplification: show the
//      configured `cfg.filters` list (one line per FilterRule,
//      `[status] action type = pattern`) and stop there. The
//      previous round was over-engineered: it walked the live
//      message buffer, deduped rules across all suppressed rows,
//      and excluded `sender_action` rules — that's the row
//      tooltip's job. The operator wants to see what's
//      configured, not what the table is currently matching.
//
//   3. S3 bucket — round-5 filesystem navigator (ported from the
//      legacy Testing page). Fetches `/api/admin/s3-objects`,
//      builds a tree of 📁 folders and 📄 files into
//      `#s3-modal-body`, lazy-loads children on folder click via
//      `/api/admin/s3-objects?prefix=...`, and on file click
//      fetches `/api/admin/s3-object?key=...` and shows the
//      content in `#json-modal`. Click handlers use event
//      delegation on the body element so dynamically-rendered
//      buttons work without re-binding.
//
// Plus the test-injection form (§6.2) and the modal lifecycle
// (open/close/background-click/Escape).

(function () {
  "use strict";

  const root = document.querySelector("[data-dashboard-controls]");
  if (!root) return;

  // ---- Modal core ---------------------------------------------------

  const openTriggers = new Map(); // modalId -> element

  function openModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    // Capture the active trigger so we can restore focus on close.
    const active = document.activeElement;
    if (active && active !== document.body) {
      openTriggers.set(id, active);
    } else {
      openTriggers.delete(id);
    }
    modal.classList.remove("hidden");
    modal.classList.add("flex");
    // Move focus into the modal (close button first).
    const closeBtn = modal.querySelector("[data-modal-close]");
    if (closeBtn) closeBtn.focus({ preventScroll: true });
  }

  function closeModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.classList.add("hidden");
    modal.classList.remove("flex");
    const trigger = openTriggers.get(id);
    openTriggers.delete(id);
    if (trigger && typeof trigger.focus === "function") {
      trigger.focus({ preventScroll: true });
    }
    // Restore the S3 navigator when the json-modal closes IF it was
    // hidden by a file click. Set by fetchS3File(), cleared here
    // after the restore. Without this hook, closing the file viewer
    // would leave the operator looking at an empty page — they had
    // to click "S3 bucket" in the header again to get back to the
    // navigator.
    if (id === "json-modal" && restoreS3OnJsonClose) {
      restoreS3OnJsonClose = false;
      openModal("s3-modal");
    }
  }

  // Set true by fetchS3File() when it closes the S3 navigator to
  // open the file viewer. closeModal("json-modal") reads this and
  // re-opens s3-modal after the viewer is gone. Reset by the S3
  // header button click so a fresh open doesn't get clobbered by
  // a stale flag from a previous file-view sequence.
  let restoreS3OnJsonClose = false;

  function setModalBody(id, text, isError) {
    const body = document.getElementById(id);
    if (!body) return;
    body.textContent = text;
    body.classList.toggle("text-red-600", !!isError);
    body.classList.toggle("bg-red-50", !!isError);
  }

  // Background click closes any open modal.
  document.addEventListener("click", function (e) {
    const closer = e.target.closest("[data-modal-close]");
    if (closer) {
      const id = closer.getAttribute("data-modal-close");
      if (id) closeModal(id);
      return;
    }
    if (
      e.target.classList &&
      e.target.classList.contains("fixed") &&
      e.target.classList.contains("inset-0")
    ) {
      const id = e.target.id;
      if (id) closeModal(id);
    }
  });

  // Escape closes the topmost modal (focus-trap light: any modal
  // open at the time of keydown is closed).
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    const openModalEl = document.querySelector(
      "#json-modal:not(.hidden), #cfg-modal:not(.hidden), #filters-modal:not(.hidden), #s3-modal:not(.hidden)"
    );
    if (openModalEl) closeModal(openModalEl.id);
  });

  // ---- PyProxy → plain JS converter --------------------------------
  //
  // The MessageManager's SignConfig instance is exposed to JS as a
  // PyProxy. `proxy.to_dict()` returns a Python dict whose keys are
  // NOT enumerable as JS properties — `Object.keys`, `for-in`,
  // `JSON.stringify(proxy)`, and `proxy[key]` all return empty /
  // undefined.
  //
  // Pyodide ships `PyProxy.toJs(...)` which does a one-shot deep
  // conversion to plain JS values. We try that first; it correctly
  // walks nested dicts/lists/datatypes/scalars and produces a plain
  // object graph that JSON.stringify can serialize.
  //
  // Fallback: when `toJs` is missing or throws (older Pyodide,
  // specific edge cases), we walk the proxy ourselves using the
  // Python iteration protocol. `Array.from(dictProxy)` yields the
  // keys as strings, `dictProxy.get(key)` reads each value. Each
  // nested value is then recursively converted. The recursion is
  // bounded (depth 6) so a cyclic reference doesn't loop forever.
  function proxyToJs(value, depth) {
    if (value == null) return value;
    if (depth > 6) return "[max depth]";
    const t = typeof value;
    if (t === "string" || t === "number" || t === "boolean") return value;
    // Pyodide's native deep converter — produces a plain JS
    // value with no PyProxy references remaining. Preferred
    // because it handles nested dataclasses, lists, dicts, and
    // scalars in a single pass.
    if (t === "object" && typeof value.toJs === "function") {
      try {
        return value.toJs({ dict_converter: Object.fromEntries });
      } catch (_) { /* fall through to manual walk */ }
    }
    // Manual fallback for objects without toJs (plain dicts etc).
    // Try iterating via the Python iterator protocol — works for
    // both dicts (yields keys) and lists (yields elements).
    if (t === "object") {
      let iterKeys = null;
      try { iterKeys = Array.from(value); } catch (_) { iterKeys = null; }
      if (iterKeys && Array.isArray(iterKeys)) {
        // Dict-like: every iteration element is a string and the
        // value has `.get(key)`. Build a plain object.
        if (
          iterKeys.length > 0 &&
          iterKeys.every(function (k) { return typeof k === "string"; }) &&
          typeof value.get === "function"
        ) {
          const out = {};
          for (let i = 0; i < iterKeys.length; i++) {
            const k = iterKeys[i];
            let v;
            try { v = value.get(k); } catch (_) { v = undefined; }
            out[k] = proxyToJs(v, depth + 1);
          }
          return out;
        }
        // List-like: each element may be its own proxy.
        const out = [];
        for (let i = 0; i < iterKeys.length; i++) {
          out.push(proxyToJs(iterKeys[i], depth + 1));
        }
        return out;
      }
    }
    // Last resort: opaque Python object — render its string form.
    try {
      const s = String(value);
      return s === "[object Object]" ? "(unreadable proxy)" : s;
    } catch (_) {
      return "(unreadable proxy)";
    }
  }

  // ---- Diagnostic modal triggers -----------------------------------

  const btnCfg = document.getElementById("btn-modal-config");
  const btnFilters = document.getElementById("btn-modal-filters");
  const btnS3 = document.getElementById("btn-modal-s3");

  if (btnCfg) {
    btnCfg.addEventListener("click", async function () {
      // Current config — read the live SignConfig the simulator is
      // applying, then walk its `to_dict()` shape into a plain JS
      // object so `JSON.stringify` can render it. The proxy enumeration
      // pattern (Array.from + .get) is the only reliable way to read
      // Python dict proxies in PyScript — see `proxyToJs` above.
      setModalBody("cfg-modal-body", "Loading…", false);
      openModal("cfg-modal");
      try {
        const App = window.App;
        if (!App || typeof App.getConfig !== "function") {
          setModalBody(
            "cfg-modal-body",
            "App not ready (PyScript bootstrap hasn't installed the config proxy yet).",
            true,
          );
          return;
        }
        const cfg = await App.getConfig();
        if (!cfg) {
          setModalBody("cfg-modal-body", "(no config loaded)", false);
          return;
        }
        let cfgDict;
        try {
          cfgDict = typeof cfg.to_dict === "function" ? cfg.to_dict() : cfg;
        } catch (_) { cfgDict = cfg; }
        const plain = proxyToJs(cfgDict, 0);
        if (plain == null || (typeof plain === "object" && Object.keys(plain).length === 0)) {
          setModalBody("cfg-modal-body", "(no config loaded)", false);
          return;
        }
        setModalBody(
          "cfg-modal-body",
          JSON.stringify(plain, null, 2),
          false,
        );
      } catch (e) {
        setModalBody(
          "cfg-modal-body",
          "Failed to fetch current config: " +
            ((e && e.message) || String(e)),
          true,
        );
      }
    });
  }

  if (btnFilters) {
    btnFilters.addEventListener("click", async function () {
      // Suppression reasons — round-5 simplification. Show the
      // OPERATOR-PINNED filter list from the config (one line per
      // rule: `[status] action type = pattern`). Nothing more — the
      // "deduped rules across live suppressed rows" approach was
      // over-engineered; the table below already shows which rows
      // are suppressed, and the per-row tooltip explains the
      // sender-not-allowed case. The modal's job is to show
      // what's CONFIGURED so the operator can audit it.
      setModalBody("filters-modal-body", "Loading…", false);
      openModal("filters-modal");
      try {
        const App = window.App;
        if (!App || typeof App.getConfig !== "function") {
          setModalBody(
            "filters-modal-body",
            "App not ready (PyScript bootstrap hasn't installed the config proxy yet).",
            true,
          );
          return;
        }
        const cfg = await App.getConfig();
        if (!cfg) {
          setModalBody("filters-modal-body", "(no config loaded)", false);
          return;
        }
        let cfgDict;
        try {
          cfgDict = typeof cfg.to_dict === "function" ? cfg.to_dict() : cfg;
        } catch (_) { cfgDict = cfg; }
        // `cfg.filters` is a list[FilterRule]; to_dict() flattens
        // each rule to {type, pattern, action, status}. Read via
        // .get() so the dict-proxy enumeration works.
        const filters = cfgDict && typeof cfgDict.get === "function"
          ? cfgDict.get("filters")
          : null;
        if (!filters || (typeof filters.length !== "number") || filters.length === 0) {
          setModalBody("filters-modal-body", "(no filters configured)", false);
          return;
        }
        const lines = [];
        for (let i = 0; i < filters.length; i++) {
          const f = filters[i];
          if (f == null) continue;
          // Each filter is a Python dict proxy (because to_dict()
          // returns plain dicts). `.get(key)` works.
          const type = (f && typeof f.get === "function") ? f.get("type") : (f && f.type);
          const pattern = (f && typeof f.get === "function") ? f.get("pattern") : (f && f.pattern);
          const action = (f && typeof f.get === "function") ? f.get("action") : (f && f.action);
          const status = (f && typeof f.get === "function") ? f.get("status") : (f && f.status);
          lines.push(
            "[" + (status || "?") + "] " +
            (action || "?") + " " +
            (type || "?") + " = " +
            (pattern || "")
          );
        }
        if (lines.length === 0) {
          setModalBody("filters-modal-body", "(no filters configured)", false);
          return;
        }
        setModalBody("filters-modal-body", lines.join("\n"), false);
      } catch (e) {
        setModalBody(
          "filters-modal-body",
          "Failed to fetch suppression reasons: " +
            ((e && e.message) || String(e)),
          true,
        );
      }
    });
  }

  if (btnS3) {
    btnS3.addEventListener("click", async function () {
      // S3 bucket filesystem navigator (round-5, 2026-07-23) —
      // re-introduced from the legacy Testing page. Fetches
      // `/api/admin/s3-objects`, builds a tree of 📁 folders and
      // 📄 files into `#s3-modal-body` (a `<div>` so nested `<ul>`
      // elements render), and wires event-delegated click handlers
      // for folder drilldown and file viewing.
      // Reset the restore-on-json-close flag: opening s3-modal
      // directly from the header means the user wants a fresh
      // navigator session, not a stale flag from a previous
      // file-view sequence that would auto-restore later.
      restoreS3OnJsonClose = false;
      const body = document.getElementById("s3-modal-body");
      if (!body) return;
      body.innerHTML = '<div class="text-slate-500 text-sm p-2">Loading…</div>';
      openModal("s3-modal");
      try {
        body.innerHTML = await renderS3Nodes("");
      } catch (e) {
        body.innerHTML = '<div class="text-red-500 text-sm p-2">Failed to load S3 bucket: ' +
          escapeHtml((e && e.message) || String(e)) + "</div>";
      }
    });
  }

  // ---- S3 navigator helpers ----------------------------------------

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Escape a prefix for use as a CSS attribute-selector value.
  // Attribute selectors need backslash-escaped quotes; this is the
  // minimum we need to do `querySelector('[data-s3-children="..."]')`
  // safely. Folders can contain '/' which is fine; quotes in keys
  // would be unusual but we still handle them.
  function cssEscapeAttr(s) {
    return String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  // Render the list of nodes returned for `prefix` (empty string
  // = root) as an HTML `<ul>` tree. Folders become expandable
  // buttons with a nested empty `<ul>` that lazy-loads on click;
  // files become buttons that fetch and display content.
  async function renderS3Nodes(prefix) {
    const url = "/api/admin/s3-objects" +
      (prefix ? "?prefix=" + encodeURIComponent(prefix) : "");
    const res = await fetch(url, { credentials: "same-origin" });
    if (!res.ok) {
      throw new Error("HTTP " + res.status + " " + res.statusText);
    }
    const data = await res.json();
    const nodes = (data && Array.isArray(data.nodes)) ? data.nodes : [];
    if (nodes.length === 0) {
      return '<div class="text-slate-400 text-sm p-2">(empty)</div>';
    }
    return '<ul class="space-y-1">' + nodes.map(function (node) {
      if (node.id.endsWith("/")) {
        // Folder — expandable. The nested <ul> starts hidden and
        // empty; clicking the folder button fills it via
        // renderS3Nodes(prefix) on first expand.
        return (
          '<li>' +
            '<button type="button" data-s3-action="folder" data-prefix="' +
              escapeHtml(node.id) + '"' +
              ' class="flex items-center gap-2 px-2 py-1 rounded hover:bg-indigo-50 text-sm cursor-pointer w-full text-left">' +
              '<span class="text-slate-400">📁</span>' +
              escapeHtml(node.text) +
            '</button>' +
            '<ul class="ml-4 hidden" data-s3-children="' +
              escapeHtml(cssEscapeAttr(node.id)) + '"></ul>' +
          '</li>'
        );
      }
      // File — click to view.
      return (
        '<li>' +
          '<button type="button" data-s3-action="file" data-key="' +
            escapeHtml(node.id) + '"' +
            ' class="flex items-center gap-2 px-2 py-1 rounded hover:bg-indigo-50 text-sm cursor-pointer w-full text-left">' +
            '<span class="text-slate-400">📄</span>' +
            escapeHtml(node.text) +
          '</button>' +
        '</li>'
      );
    }).join("") + '</ul>';
  }

  // Event delegation on the s3-modal-body. Folder clicks toggle
  // their child `<ul>` and lazy-load it on first expand; file
  // clicks fetch the object content and show it in json-modal.
  const s3Body = document.getElementById("s3-modal-body");
  if (s3Body) {
    s3Body.addEventListener("click", async function (ev) {
      const folderBtn = ev.target.closest('[data-s3-action="folder"]');
      if (folderBtn) {
        const prefix = folderBtn.getAttribute("data-prefix");
        if (!prefix) return;
        // The child <ul> sits next to the button in the same <li>.
        // Find by traversing up to the <li> and looking for the
        // nested children element with the matching data attr.
        const li = folderBtn.closest("li");
        if (!li) return;
        const childList = li.querySelector(
          '[data-s3-children="' + cssEscapeAttr(prefix) + '"]'
        );
        if (!childList) return;
        if (childList.classList.contains("hidden")) {
          childList.classList.remove("hidden");
          if (childList.innerHTML.trim() === "") {
            childList.innerHTML =
              '<li class="text-slate-400 text-sm p-2">Loading…</li>';
            try {
              childList.innerHTML = await renderS3Nodes(prefix);
            } catch (e) {
              childList.innerHTML =
                '<li class="text-red-500 text-sm p-2">Error: ' +
                escapeHtml((e && e.message) || String(e)) + "</li>";
            }
          }
        } else {
          childList.classList.add("hidden");
        }
        return;
      }
      const fileBtn = ev.target.closest('[data-s3-action="file"]');
      if (fileBtn) {
        const key = fileBtn.getAttribute("data-key");
        if (!key) return;
        fetchS3File(key);
        return;
      }
    });
  }

  async function fetchS3File(key) {
    // Hide the S3 navigator before showing the file content so the
    // json-modal sits on top of an empty overlay instead of fighting
    // z-index with the navigator. Both modals use z-50, so leaving
    // s3-modal open stacks the file viewer BEHIND the navigator.
    // The navigator is restored automatically when json-modal closes
    // (see closeModal's restore hook); set the flag here so that
    // hook knows to re-open it.
    closeModal("s3-modal");
    restoreS3OnJsonClose = true;
    try {
      const res = await fetch(
        "/api/admin/s3-object?key=" + encodeURIComponent(key),
        { credentials: "same-origin" }
      );
      const obj = await res.json();
      const titleEl = document.getElementById("json-modal-title");
      if (titleEl) titleEl.textContent = key;
      if (obj && obj.error) {
        setModalBody("json-modal-body", "Error: " + obj.error, true);
        openModal("json-modal");
        return;
      }
      let formatted = obj && obj.content;
      if (formatted) {
        // Try to pretty-print JSON; fall back to raw text otherwise.
        try { formatted = JSON.stringify(JSON.parse(formatted), null, 2); }
        catch (_) { /* keep raw */ }
      }
      setModalBody("json-modal-body", formatted || "(empty)", false);
      openModal("json-modal");
    } catch (e) {
      const titleEl = document.getElementById("json-modal-title");
      if (titleEl) titleEl.textContent = key;
      setModalBody(
        "json-modal-body",
        "Failed to fetch S3 object: " + ((e && e.message) || String(e)),
        true,
      );
      openModal("json-modal");
    }
  }

  // ---- Test injection (§6.2) ---------------------------------------

  const injectForm = document.getElementById("inject-form");
  const injectBody = document.getElementById("inject-body");
  const injectSender = document.getElementById("inject-sender");
  const injectResult = document.getElementById("inject-result");
  if (injectForm && injectBody) {
    injectForm.addEventListener("submit", async function (ev) {
      ev.preventDefault();
      if (!injectResult) return;
      injectResult.textContent = "Sending…";
      injectResult.classList.remove("text-red-600", "text-green-600");
      const body = injectBody.value.trim();
      const sender = (injectSender && injectSender.value.trim()) || "+15551234567";
      if (!body) {
        injectResult.textContent = "Body is required";
        injectResult.classList.add("text-red-600");
        return;
      }
      try {
        const params = new URLSearchParams();
        params.set("From", sender);
        params.set("Body", body);
        const res = await fetch("/api/test-messages", {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-API-Key": (window.APP_CONFIG || {}).apiKey || "",
          },
          body: params.toString(),
        });
        const text = await res.text();
        if (!res.ok) {
          injectResult.classList.add("text-red-600");
          injectResult.textContent = `Flask rejected: HTTP ${res.status} — ${text}`;
          // §6.1: no optimistic message on HTTP failure. The form
          // does NOT push a row into the recent-100 table; the row
          // appears only when MQTT actually delivers the envelope.
          return;
        }
        injectResult.classList.add("text-green-600");
        injectResult.textContent = `Flask accepted: HTTP ${res.status} — waiting for MQTT receipt…`;
        // Clear the body field so the operator can inject the next
        // message; keep the sender so consecutive injections are
        // fast.
        injectBody.value = "";
        injectBody.focus();
      } catch (e) {
        injectResult.classList.add("text-red-600");
        injectResult.textContent = `Network error: ${(e && e.message) || e}`;
      }
    });
  }

  console.log("[dashboard-modals] bound");
})();
