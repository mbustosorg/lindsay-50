// Dashboard recent-100 message table (issue #48, §7).
//
// The table is the wire shape of the in-browser MessageManager's
// view: up to 100 records (suppressed included), with `MessageView`
// (sender, body, source, media, rules) preserved verbatim. The
// 100-record ring lives in `window.App.getMessages(100, true)`; the
// table is rebuilt from scratch on every change-notification so the
// pagination state stays correct after live MQTT receipts (§7.3:
// page clamping after live updates).
//
// Pagination (§7.4): 20 rows per page, client-side, no extra
// history requests. Source badges (§7.5) reuse the same shape as
// the Testing page feed (`source === "rest"` → "REST seed",
// `source === "mqtt"` → "MQTT live"). Suppression actions (§7.8)
// call the same authenticated endpoints the Testing page uses and
// never reload the document.

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
    // §7.5: distinct REST seed vs MQTT live badges. The
    // MessageView.source field is the SSOT.
    if (source === "rest") {
      return '<span class="px-2 py-1 rounded-full bg-indigo-100 text-indigo-700 text-xs font-semibold">REST seed</span>';
    }
    if (source === "mqtt") {
      return '<span class="px-2 py-1 rounded-full bg-green-100 text-green-700 text-xs font-semibold">MQTT live</span>';
    }
    return '<span class="px-2 py-1 rounded-full bg-slate-100 text-slate-600 text-xs font-semibold">' +
      (source || "unknown") +
      "</span>";
  }

  function ruleChips(rules) {
    if (!Array.isArray(rules) || rules.length === 0) return "";
    return rules
      .map(function (r) {
        const label = (r && (r.label || r.name || r.id)) || "rule";
        return (
          '<span class="ml-2 px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 text-xs">' +
          escapeHtml(String(label)) +
          "</span>"
        );
      })
      .join("");
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
    const suppressedBadge = suppressed
      ? '<span class="ml-2 px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 text-xs">suppressed</span>'
      : "";
    const actionLabel = suppressed ? "Unsuppress" : "Suppress";
    const actionState = suppressed ? "active" : "suppressed";
    tr.innerHTML =
      '<td class="px-4 py-3 font-mono text-xs text-slate-500 whitespace-nowrap">' +
      escapeHtml(sender) +
      "</td>" +
      '<td class="px-4 py-3"><div class="text-slate-700 truncate max-w-md">' +
      escapeHtml(body) +
      mediaBadge +
      suppressedBadge +
      ruleChips(rules) +
      "</div></td>" +
      '<td class="px-4 py-3">' +
      sourceBadge(source) +
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
    try {
      // §7.1: 100 records with suppressed records included.
      const rows = App.getMessages(100, true) || [];
      currentRows = rows;
      render();
    } catch (e) {
      console.warn("[dashboard-recent] getMessages failed:", e);
      currentRows = [];
      render();
    }
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
  tbody.addEventListener("click", async function (ev) {
    const btn = ev.target.closest("[data-suppress-id]");
    if (!btn) return;
    ev.preventDefault();
    const id = btn.getAttribute("data-suppress-id");
    const target = btn.getAttribute("data-suppress-target"); // "suppressed" or "active"
    if (!id) return;
    const endpoint =
      target === "suppressed"
        ? "/api/admin/suppress-message"
        : "/api/admin/unsuppress-message";
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = target === "suppressed" ? "Suppressing…" : "Unsuppressing…";
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": (window.APP_CONFIG || {}).apiKey || "",
        },
        body: JSON.stringify({ id: id }),
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
      // Authoritative refresh from the server.
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