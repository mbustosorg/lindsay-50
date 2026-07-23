// Dashboard recent-100 message table (issue #48, §7).
//
// The table is the wire shape of the in-browser MessageManager's
// view: up to 100 records (suppressed included), with `MessageView`
// (sender, body, source, media, rules, sender_name, suppressed)
// preserved verbatim. The 100-record ring lives in
// `window.App.getMessages(100, true)`; the table is rebuilt from
// scratch on every change-notification so the pagination state
// stays correct after live MQTT receipts (§7.3: page clamping
// after live updates).
//
// Column order (2026-07-23): Source | Sender | Body | Status |
// Received | Actions. Source moved to the far left per operator
// request — the legacy Testing page also led with the source
// badge. The status column shows the suppression verdict
// (suppressed/visible) plus the rule chips that caused it
// (keyword / regex / message / sender_action); both shapes
// match the legacy Testing feed.
//
// Pagination (§7.4): 20 rows per page, client-side, no extra
// history requests. Source badges (§7.5) reuse the same shape as
// the Testing page feed (`source === "rest"` → "REST",
// `source === "mqtt"` → "MQTT"). Suppression actions (§7.8)
// call the same authenticated endpoints the Testing page uses
// and never reload the document.

(function () {
  "use strict";

  const PAGE_SIZE = 20;

  const root = document.querySelector("[data-dashboard-controls]");
  if (!root) return;

  const tbody = document.getElementById("recent-tbody");
  const pageInfo = document.getElementById("recent-page-info");
  const prevBtn = document.getElementById("recent-prev");
  const nextBtn = document.getElementById("recent-next");
  if (!tbody) return;

  let currentRows = [];
  let currentPage = 0;

  function pageCount() {
    return Math.max(1, Math.ceil(currentRows.length / PAGE_SIZE));
  }

  function clampPage() {
    const max = pageCount() - 1;
    if (currentPage > max) currentPage = max;
    if (currentPage < 0) currentPage = 0;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    } catch (_) {
      return iso;
    }
  }

  function sourceBadge(source) {
    // §7.5: distinct REST vs MQTT badges. The MessageView.source
    // field is the SSOT. Short labels (REST / MQTT) so the
    // leftmost column stays narrow — the operator requested a
    // compact left column on 2026-07-23.
    if (source === "rest") {
      return '<span class="px-2 py-1 rounded-full bg-indigo-100 text-indigo-700 text-xs font-semibold">REST</span>';
    }
    if (source === "mqtt") {
      return '<span class="px-2 py-1 rounded-full bg-green-100 text-green-700 text-xs font-semibold">MQTT</span>';
    }
    return '<span class="px-2 py-1 rounded-full bg-slate-100 text-slate-600 text-xs font-semibold">' +
      (source || "unknown") +
      "</span>";
  }

  function ruleChips(rules) {
    // FilterRule.to_dict() produces {type, pattern, action, status}.
    // Display each rule's `type` (keyword / regex / message /
    // sender_action) so the operator can see WHICH rule suppressed
    // the row, not just that it was suppressed. The legacy Testing
    // page rendered the same `r.type` field.
    if (!Array.isArray(rules) || rules.length === 0) return "";
    return rules
      .map(function (r) {
        const label = (r && r.type) || "rule";
        return (
          '<span class="ml-1 px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 text-xs font-mono">' +
          escapeHtml(String(label)) +
          "</span>"
        );
      })
      .join("");
  }

  function statusCell(suppressed, rules) {
    // Legacy Testing feed rendered `suppressed` as a red pill and
    // `visible` as a green pill, with the matching rule types
    // underneath. Match the shape so the operator gets the same
    // info on the dashboard that they used to get on /testing.
    if (suppressed) {
      return (
        '<div class="flex flex-col gap-0.5">' +
        '<span class="inline-block w-fit px-2 py-0.5 rounded-full bg-red-100 text-red-700 text-xs font-semibold">suppressed</span>' +
        ruleChips(rules) +
        "</div>"
      );
    }
    return '<span class="inline-block w-fit px-2 py-0.5 rounded-full bg-green-100 text-green-700 text-xs font-semibold">visible</span>';
  }

  function senderCell(sender, senderName) {
    // The MessageView's `sender_name` is the operator-named
    // identity (resolved by lib_shared/messages._enrich_entries
    // against cfg.senders). Render "<name>" if we have one,
    // else fall back to the raw phone. The phone stays in a
    // monospaced subtitle so it's still copy-paste-able for
    // debugging.
    const phone = escapeHtml(sender || "");
    if (senderName && String(senderName).trim()) {
      return (
        '<div class="flex flex-col">' +
        '<span class="text-slate-700 text-sm font-semibold">' +
        escapeHtml(String(senderName)) +
        "</span>" +
        '<span class="text-slate-400 text-xs font-mono">' +
        phone +
        "</span>" +
        "</div>"
      );
    }
    return '<span class="font-mono text-xs text-slate-500 whitespace-nowrap">' + phone + "</span>";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderRow(msg) {
    const id = (msg && msg.id) || "";
    const sender = (msg && msg.sender) || "";
    const senderName = (msg && msg.sender_name) || "";
    const body = (msg && msg.body) || "";
    const source = (msg && msg.source) || "";
    const receivedAt = (msg && msg.received_at) || (msg && msg.receivedAt) || "";
    const suppressed = !!(msg && msg.suppressed);
    const rules = (msg && msg.rules) || [];
    const mediaCount = Array.isArray(msg && msg.media) ? msg.media.length : 0;
    const tr = document.createElement("tr");
    tr.setAttribute("data-msg-id", id);
    tr.setAttribute("data-received-at", receivedAt);
    if (suppressed) tr.classList.add("opacity-60");
    tr.className =
      "hover:bg-indigo-50/30 transition-colors" +
      (suppressed ? " opacity-60" : "");
    const mediaBadge = mediaCount
      ? '<span class="ml-2 px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 text-xs">' +
        mediaCount +
        " media</span>"
      : "";
    const actionLabel = suppressed ? "Unsuppress" : "Suppress";
    const actionState = suppressed ? "active" : "suppressed";
    // Column order (2026-07-23): Source | Sender | Body | Status |
    // Received | Actions. Source moved to the far-left per
    // operator request. Status column replaced the inline
    // suppressed-badge-in-body pattern with a dedicated cell so
    // suppression state is visible at a glance.
    tr.innerHTML =
      '<td class="px-4 py-3 whitespace-nowrap">' +
      sourceBadge(source) +
      "</td>" +
      '<td class="px-4 py-3">' +
      senderCell(sender, senderName) +
      "</td>" +
      '<td class="px-4 py-3"><div class="text-slate-700 truncate max-w-md">' +
      escapeHtml(body) +
      mediaBadge +
      "</div></td>" +
      '<td class="px-4 py-3">' +
      statusCell(suppressed, rules) +
      "</td>" +
      '<td class="px-4 py-3 text-xs text-slate-500 whitespace-nowrap">' +
      escapeHtml(fmtDate(receivedAt)) +
      "</td>" +
      '<td class="px-4 py-3 text-right">' +
      '<button data-suppress-id="' +
      escapeHtml(id) +
      '" data-suppress-target="' +
      actionState +
      '" class="px-3 py-1 rounded-lg bg-white border border-slate-200 text-xs font-semibold text-slate-700 hover:bg-indigo-50">' +
      actionLabel +
      "</button>" +
      "</td>";
    return tr;
  }

  function render() {
    clampPage();
    tbody.innerHTML = "";
    if (currentRows.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        '<td colspan="5" class="px-4 py-8 text-center text-slate-400">No messages yet.</td>';
      tbody.appendChild(tr);
      if (pageInfo) pageInfo.textContent = "No records loaded";
      if (prevBtn) prevBtn.disabled = true;
      if (nextBtn) nextBtn.disabled = true;
      return;
    }
    const start = currentPage * PAGE_SIZE;
    const end = Math.min(start + PAGE_SIZE, currentRows.length);
    const slice = currentRows.slice(start, end);
    const frag = document.createDocumentFragment();
    slice.forEach(function (msg) {
      frag.appendChild(renderRow(msg));
    });
    tbody.appendChild(frag);
    if (pageInfo) {
      pageInfo.textContent =
        "Showing " +
        (start + 1) +
        "–" +
        end +
        " of " +
        currentRows.length +
        " (page " +
        (currentPage + 1) +
        " of " +
        pageCount() +
        ")";
    }
    if (prevBtn) prevBtn.disabled = currentPage === 0;
    if (nextBtn) nextBtn.disabled = currentPage >= pageCount() - 1;
  }

  function reload() {
    const App = window.App;
    if (!App || typeof App.getMessages !== "function") {
      // `app.js` may not be loaded yet; try again on the next tick.
      window.setTimeout(reload, 50);
      return;
    }
    // §7.1: 100 records with suppressed records included. The
    // second parameter is `suppress: bool = True` (see
    // lib_shared/messages.py:292) — passing `True` EXCLUDES
    // suppressed entries; we want them included so the operator
    // can see and undo their previous Suppress action. The legacy
    // Testing page used `False` here for the same reason.
    //
    // `App.getMessages` is async (returns a Promise) until PyScript
    // overwrites it with the per-generation synchronous proxy during
    // bootstrap. We need to handle both shapes so the table doesn't
    // flash `currentRows.slice is not a function` on cold load —
    // Promise objects have no `.slice`. Once PyScript is up the proxy
    // returns a true JS Array; before that, treat the function as
    // async and re-render after it settles.
    let result;
    try {
      result = App.getMessages(100, false);
    } catch (e) {
      console.warn("[dashboard-recent] getMessages failed:", e);
      currentRows = [];
      render();
      return;
    }
    if (result && typeof result.then === "function") {
      result.then(
        (rows) => {
          currentRows = coerceRows(rows);
          render();
        },
        (err) => {
          console.warn("[dashboard-recent] getMessages rejected:", err);
          currentRows = [];
          render();
        },
      );
      return;
    }
    currentRows = coerceRows(result);
    render();
  }

  function coerceRows(rows) {
    // Coerce the result of `App.getMessages(...)` into a real JS Array.
    // PyScript's `to_js(list)` returns a PyProxy of a JS Array (has
    // `.length` and indexed access, but no `.slice`); Python-side
    // pre-bootstrap paths can also return `null` or a plain object.
    // `Array.isArray` rejects all of these. `Array.from` works for
    // array-like objects (those with `.length`); fall back to `[]`
    // for anything else.
    if (Array.isArray(rows)) return rows;
    if (rows && typeof rows.length === "number") {
      try {
        return Array.from(rows);
      } catch (_) {
        return [];
      }
    }
    return [];
  }

  if (prevBtn) {
    prevBtn.addEventListener("click", function () {
      if (currentPage > 0) {
        currentPage -= 1;
        render();
      }
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener("click", function () {
      if (currentPage < pageCount() - 1) {
        currentPage += 1;
        render();
      }
    });
  }

  // §7.8: suppress / unsuppress actions are single-flight per row
  // and never reload the document. The button is disabled during
  // the in-flight POST so a double-click can't double-publish.
  //
  // Endpoint shape (verified against main.py:693 + main.py:716):
  //   POST /api/messages/<msg_id>/suppress     — add a type=message
  //     filter rule that suppresses the given message
  //   POST /api/messages/<msg_id>/unsuppress   — remove the matching
  //     type=message filter rule
  // Both take the msg_id in the URL PATH, not in a JSON body. The
  // legacy /api/admin/suppress-message route the dashboard
  // previously called does not exist — that 404 was the operator
  // report on 2026-07-23.
  tbody.addEventListener("click", async function (ev) {
    const btn = ev.target.closest("[data-suppress-id]");
    if (!btn) return;
    ev.preventDefault();
    const id = btn.getAttribute("data-suppress-id");
    const target = btn.getAttribute("data-suppress-target"); // "suppressed" or "active"
    if (!id) return;
    const endpoint =
      target === "suppressed"
        ? "/api/messages/" + encodeURIComponent(id) + "/suppress"
        : "/api/messages/" + encodeURIComponent(id) + "/unsuppress";
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = target === "suppressed" ? "Suppressing…" : "Unsuppressing…";
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "X-API-Key": (window.APP_CONFIG || {}).apiKey || "",
        },
      });
      if (!res.ok) {
        const text = await res.text().catch(function () { return ""; });
        console.error(
          "[dashboard-recent] suppress action failed: HTTP",
          res.status,
          text
        );
        // Non-destructive: re-enable the button so the operator can
        // retry. We do NOT reload the document.
        btn.disabled = false;
        btn.textContent = originalLabel;
        return;
      }
      // Authoritative refresh from the server. `reload()` calls
      // `App.getMessages(100, true)` which re-runs the suppression
      // filter against the freshly added/removed rule.
      reload();
    } catch (e) {
      console.error("[dashboard-recent] suppress action network error:", e);
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  });

  // Subscribe to change-notifications so the table refreshes on
  // every MQTT receipt. `window.App.registerOnChange` is the same
  // API the Testing page uses.
  function subscribe() {
    const App = window.App;
    if (!App) {
      window.setTimeout(subscribe, 50);
      return;
    }
    if (typeof App.registerOnChange === "function") {
      App.registerOnChange(function () {
        reload();
      });
    }
    // Initial render.
    reload();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", subscribe);
  } else {
    subscribe();
  }

  console.log("[dashboard-recent] bound");
})();