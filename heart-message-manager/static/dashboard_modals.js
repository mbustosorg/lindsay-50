// Dashboard modals + test injection (issue #48, §6).
//
// Three responsibilities:
//   1. Wire the three diagnostic modal triggers (Current config,
//      Active filters, S3 bucket) to fetch their data and open the
//      modal shell. The data fetchers delegate to the existing
//      authenticated `/api/admin/config`, `/api/admin/filters`,
//      `/api/admin/s3-objects` endpoints (the same ones the Testing
//      page uses).
//   2. Bind the test-injection form to POST `/api/test-messages`
//      with the same shape the Testing page uses, then surface the
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

  function setModalBody(id, text, isError) {
    const body = document.getElementById(id + "-body");
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

  // ---- Diagnostic modal triggers -----------------------------------

  async function fetchAndShow(modalId, bodyId, url, errorPrefix) {
    setModalBody(bodyId, "Loading…", false);
    openModal(modalId);
    try {
      const res = await fetch(url, {
        headers: { "X-API-Key": (window.APP_CONFIG || {}).apiKey || "" },
      });
      const text = await res.text();
      if (!res.ok) {
        setModalBody(bodyId, `${errorPrefix}: HTTP ${res.status}\n${text}`, true);
        return;
      }
      // Pretty-print JSON if it parses; otherwise show the raw body.
      try {
        const parsed = JSON.parse(text);
        setModalBody(bodyId, JSON.stringify(parsed, null, 2), false);
      } catch (_) {
        setModalBody(bodyId, text, false);
      }
    } catch (e) {
      setModalBody(bodyId, `${errorPrefix}: ${(e && e.message) || e}`, true);
    }
  }

  const btnCfg = document.getElementById("btn-modal-config");
  const btnFilters = document.getElementById("btn-modal-filters");
  const btnS3 = document.getElementById("btn-modal-s3");

  if (btnCfg) {
    btnCfg.addEventListener("click", function () {
      fetchAndShow(
        "cfg-modal",
        "cfg",
        "/api/admin/config",
        "Failed to fetch current config"
      );
    });
  }
  if (btnFilters) {
    btnFilters.addEventListener("click", function () {
      fetchAndShow(
        "filters-modal",
        "filters",
        "/api/admin/filters",
        "Failed to fetch active filters"
      );
    });
  }
  if (btnS3) {
    btnS3.addEventListener("click", async function () {
      setModalBody("s3-modal-body", "", false);
      // Render the body as a flat list of object keys + sizes.
      const target = document.getElementById("s3-modal-body");
      if (target) target.textContent = "Loading…";
      openModal("s3-modal");
      try {
        const res = await fetch("/api/admin/s3-objects", {
          headers: { "X-API-Key": (window.APP_CONFIG || {}).apiKey || "" },
        });
        const text = await res.text();
        if (!res.ok) {
          if (target) {
            target.classList.add("text-red-600");
            target.textContent = `Failed to fetch S3 objects: HTTP ${res.status}\n${text}`;
          }
          return;
        }
        const parsed = JSON.parse(text);
        const items = (parsed && parsed.objects) || parsed || [];
        if (target) {
          target.classList.remove("text-red-600");
          if (!Array.isArray(items) || items.length === 0) {
            target.textContent = "(no objects)";
            return;
          }
          const list = items
            .map(function (o) {
              const key = (o && o.Key) || (o && o.key) || String(o);
              const size = (o && o.Size) || (o && o.size) || "";
              return size
                ? `${key}    (${size} bytes)`
                : `${key}`;
            })
            .join("\n");
          target.textContent = list;
        }
      } catch (e) {
        if (target) {
          target.classList.add("text-red-600");
          target.textContent = `Failed to fetch S3 objects: ${(e && e.message) || e}`;
        }
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