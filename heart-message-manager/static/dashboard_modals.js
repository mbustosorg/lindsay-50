// Dashboard modals + test injection (issue #48, §6).
//
// Three responsibilities:
//   1. Wire the two diagnostic modal triggers (Current config,
//      Active filters) to fetch their data and open the modal shell.
//      Both read from the in-memory live config
//      (`window.App.getConfig()`) — the `/api/admin/config` endpoint
//      returns the server-side admin view, which can drift from what
//      the Pi simulator is actually running. The simulator's
//      `MessageManager` is the source of truth for "what config is
//      currently being applied to the EffectsCoordinator", and that's
//      already mirrored into `App.getConfig()` by the per-generation
//      bootstrap.
//      The S3 bucket trigger was removed on 2026-07-23: its
//      endpoint's response shape (jstree-style `nodes: [...]`) didn't
//      match the modal's `objects: [...]` parser, so the modal
//      displayed "Failed to fetch S3 objects" with no useful
//      diagnostic. The legacy Testing page was removed in the same
//      sweep — the diagnostic surface that lived there has been
//      folded into the dashboard's test-injection form (§6.2).
//   2. Bind the test-injection form to POST `/api/test-messages`
//      with the same shape the Testing page used, then surface the
//      result (HTTP status + parsed body) in the inline result row.
//      §6.1: the form reports Flask acceptance separately from the
//      MQTT-receipt side (which lives in `preview.js` via the
//      message-link click handler), and never creates an optimistic
//      message on HTTP failure.
//   3. Manage the modal lifecycle:
//      - background click closes the modal
//      - Escape closes the modal
//      - close button (any `[data-modal-close=ID]`) closes the modal
//      - the trigger element's focus is restored on close (§6.5)
//
// §6.7: opening, updating, and closing any modal is purely a DOM
// concern — no MessageManager mutation, no coordinator tick, no MQTT
// dispatch. The shim never touches `window.Dashboard` or
// `window._coordinator`.

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
  }

  // The `id` must be the FULL body-element id as declared in the template
  // (e.g. `cfg-modal-body`). `bodyId` callers pass must therefore include
  // `-body`. The legacy `id + "-body"` concat silently no-op'd when a
  // caller passed a fully-qualified id like `cfg-modal-body`.
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
      "#json-modal:not(.hidden), #cfg-modal:not(.hidden), #filters-modal:not(.hidden)"
    );
    if (openModalEl) closeModal(openModalEl.id);
  });

  // ---- Diagnostic modal triggers -----------------------------------

  const btnCfg = document.getElementById("btn-modal-config");
  const btnFilters = document.getElementById("btn-modal-filters");

  if (btnCfg) {
    btnCfg.addEventListener("click", async function () {
      // Current config lives in the per-generation MessageManager's
      // SignConfig — the same live state the EffectsCoordinator is
      // applying. Read it via `App.getConfig()` (the proxy the
      // dashboard_bootstrap installed) instead of hitting
      // `/api/admin/config`, which returns the server-side admin
      // view and can drift from what the Pi simulator is actually
      // running. Same proxy the Active filters button reads from.
      setModalBody("cfg-modal-body", "Loading…", false);
      openModal("cfg-modal");
      try {
        const App = window.App;
        if (!App || typeof App.getConfig !== "function") {
          setModalBody(
            "cfg-modal-body",
            "Failed to fetch current config: App not ready (PyScript bootstrap hasn't installed the config proxy yet).",
            true,
          );
          return;
        }
        const cfg = await App.getConfig();
        if (!cfg || (typeof cfg === "object" && Object.keys(cfg).length === 0)) {
          setModalBody("cfg-modal-body", "(no config loaded)", false);
          return;
        }
        setModalBody("cfg-modal-body", JSON.stringify(cfg, null, 2), false);
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
      // Active filters live inside the live SignConfig the
      // dashboard already mirrors from MQTT — there's no
      // dedicated `/api/admin/filters` endpoint and creating
      // one would duplicate state. Read from the in-memory
      // buffer via `App.getConfig()` (the same proxy the
      // Testing page reads from). `App.getConfig` is async in
      // the cold-load window before PyScript overwrites the
      // stub with the per-generation proxy; await it directly.
      setModalBody("filters-modal-body", "Loading…", false);
      openModal("filters-modal");
      try {
        const App = window.App;
        if (!App || typeof App.getConfig !== "function") {
          setModalBody(
            "filters-modal-body",
            "Failed to fetch active filters: App not ready (PyScript bootstrap hasn't installed the config proxy yet).",
            true,
          );
          return;
        }
        const cfg = await App.getConfig();
        const filters =
          (cfg && Array.isArray(cfg.filters)) ? cfg.filters : [];
        if (filters.length === 0) {
          setModalBody("filters-modal-body", "(no active filters)", false);
          return;
        }
        // FilterRule.to_dict() produces {type, pattern, action, status}
        // (lib_shared/models.py:280). Render each row as a one-line
        // summary so the modal matches the same compact view the
        // Settings page uses.
        const rows = filters
          .map(function (f) {
            const type = (f && f.type) || "?";
            const pattern = (f && f.pattern) || "";
            const action = (f && f.action) || "?";
            const status = (f && f.status) || "?";
            return `[${status}] ${action} ${type} = ${pattern}`;
          })
          .join("\n");
        setModalBody("filters-modal-body", rows, false);
      } catch (e) {
        setModalBody(
          "filters-modal-body",
          "Failed to fetch active filters: " +
            ((e && e.message) || String(e)),
          true,
        );
      }
    });
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