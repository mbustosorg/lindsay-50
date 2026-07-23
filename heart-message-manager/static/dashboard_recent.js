// Dashboard recent-100 message table (issue #48, §7 + round-5
// changes 2026-07-23).
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
// Column order (2026-07-23 round-5): Source | Sender | Body |
// Received | Status | Actions. Status moved second-from-right so
// the operator can scan the action column against the suppression
// verdict without their eyes leaving the right side of the table.
//
// "Hide suppressed" toggle (2026-07-23 round-5). Default
// unselected. When toggled on, the table excludes any row whose
// `suppressed` is true. The toggle is purely a client-side filter
// — it does not call the server, the count of "100 records" is
// always 100, the table just slices a filtered view.
//
// Unsuppress gating (2026-07-23 round-5). The "Unsuppress" action
// only makes sense when the row was suppressed via a `type:"message"`
// filter rule (the legacy "Suppress" action added a per-message
// filter). When a row is suppressed for any OTHER reason — sender
// not in `cfg.senders` (synthetic `type:"sender_action"`), keyword
// match (`type:"keyword"`), or regex match (`type:"regex"`) — there
// is no per-message filter to remove, so the button is disabled
// with a tooltip explaining the real reason. The check is local:
// any of the row's `rules[].type === "message"` enables the
// button. The legacy Testing page used the same rule.
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
  const hideSuppressedEl = document.getElementById("recent-hide-suppressed");
  if (!tbody) return;

  let currentRows = [];
  let currentPage = 0;
  // Default unselected — the operator lands on a populated table
  // that shows ALL 100 records (suppressed included) so they can
  // see and undo any prior Suppress actions.
  let hideSuppressed = !!(hideSuppressedEl && hideSuppressedEl.checked);

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
    // against cfg.senders). Always render the name on top and
    // the raw phone underneath in lighter mono text — even when
    // no name was resolved (the layout stays consistent and the
    // phone is still copy-paste-able for debugging). The legacy
    // Testing page used the same stacked layout.
    const phone = escapeHtml(sender || "");
    const name = senderName && String(senderName).trim()
      ? escapeHtml(String(senderName))
      : "&nbsp;";
    return (
      '<div class="flex flex-col">' +
      '<span class="text-slate-700 text-sm font-semibold">' +
      name +
      "</span>" +
      '<span class="text-slate-400 text-xs font-mono">' +
      phone +
      "</span>" +
      "</div>"
    );
  }

  function mediaCell(media) {
    // Media column (2026-07-23 round-5) — mirrors the /messages
    // archive column. Each attachment renders a 12×12 thumb:
    //   - image/* — <img> thumbnail inside an <a> link
    //   - video/* — play badge (▶) inside an <a> link
    //   - other  — paperclip (📎) inside an <a> link
    // All three link to /api/media/<key> (Flask 302 to a freshly
    // signed S3 URL — same auth boundary the Pi's MediaCycler uses,
    // so session cookies work the same way as /messages).
    // target="_blank" + rel="noopener" matches /messages so the
    // browser's progressive-enhancement story is identical.
    if (!Array.isArray(media) || media.length === 0) {
      return '<span class="text-xs text-slate-400">—</span>';
    }
    const thumbs = media
      .map(function (item) {
        if (!item || !item.url) return "";
        const url = "/api/media/" + encodeURIComponent(item.url);
        const type = String(item.type || "");
        const title = escapeHtml(type + ": " + item.url);
        if (type.startsWith("image/")) {
          return (
            '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener"' +
            ' title="' + title + '" data-testid="media-thumb"' +
            ' class="block w-12 h-12 rounded-lg overflow-hidden bg-indigo-50 border border-indigo-100 hover:border-primary transition-all cursor-pointer">' +
            '<img src="' + escapeHtml(url) + '" alt="' + escapeHtml(item.url) + '"' +
            ' class="w-full h-full object-cover" loading="lazy">' +
            "</a>"
          );
        }
        if (type.startsWith("video/")) {
          return (
            '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener"' +
            ' title="' + title + '" data-testid="media-thumb-video"' +
            ' class="flex items-center justify-center w-12 h-12 rounded-lg bg-indigo-50 border border-indigo-100 hover:border-primary transition-all cursor-pointer text-primary">' +
            '<span class="text-xl">▶</span>' +
            "</a>"
          );
        }
        return (
          '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener"' +
          ' title="' + title + '" data-testid="media-thumb-other"' +
          ' class="flex items-center justify-center w-12 h-12 rounded-lg bg-indigo-50 border border-indigo-100 hover:border-primary transition-all cursor-pointer text-slate-500 text-xs font-mono">' +
          "📎" +
          "</a>"
        );
      })
      .join("");
    return '<div class="flex items-center gap-2 flex-wrap">' + thumbs + "</div>";
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
    const media = Array.isArray(msg && msg.media) ? msg.media : [];
    const tr = document.createElement("tr");
    tr.setAttribute("data-msg-id", id);
    tr.setAttribute("data-received-at", receivedAt);
    if (suppressed) tr.classList.add("opacity-60");
    tr.className =
      "hover:bg-indigo-50/30 transition-colors" +
      (suppressed ? " opacity-60" : "");
    // Inline media badge in the Body cell — kept for backward compat
    // with the prior round-5 render so the body still hints at
    // attached media even when the Media column is collapsed.
    const mediaBadge = media.length
      ? '<span class="ml-2 px-2 py-0.5 rounded-full bg-slate-100 text-slate-600 text-xs">' +
        media.length +
        " media</span>"
      : "";
    // Action gating (2026-07-23 round-5). "Unsuppress" only makes
    // sense when the row was suppressed via a per-message
    // `type:"message"` filter rule — that's the one the legacy
    // "Suppress" action added. For all other reasons (sender
    // not in `cfg.senders` → synthetic `type:"sender_action"`,
    // keyword match, regex match), there is no per-message
    // filter to remove, so the button is disabled with a
    // tooltip explaining the real reason.
    const hasMessageRule = Array.isArray(rules) && rules.some(
      function (r) { return r && r.type === "message"; }
    );
    const actionLabel = suppressed ? "Unsuppress" : "Suppress";
    const actionState = suppressed ? "active" : "suppressed";
    let actionAttrs = "";
    let actionClasses =
      "px-3 py-1 rounded-lg bg-white border border-slate-200 text-xs font-semibold text-slate-700 hover:bg-indigo-50";
    let actionTitle = "";
    if (suppressed && !hasMessageRule) {
      // Determine the most useful tooltip per the row's rules.
      const ruleTypes = Array.isArray(rules) ? rules.map(function (r) { return r && r.type; }).filter(Boolean) : [];
      let reason;
      if (ruleTypes.indexOf("sender_action") !== -1) {
        reason = "Suppressed because sender is not in the allow list. Add the sender in the Settings page to allow it.";
      } else if (ruleTypes.indexOf("keyword") !== -1) {
        reason = "Suppressed by a keyword filter — remove the rule in the Active filters dialog to unsuppress.";
      } else if (ruleTypes.indexOf("regex") !== -1) {
        reason = "Suppressed by a regex filter — remove the rule in the Active filters dialog to unsuppress.";
      } else {
        reason = "Cannot unsuppress — no per-message filter is attached to this row.";
      }
      actionAttrs = " disabled";
      actionClasses =
        "px-3 py-1 rounded-lg bg-slate-50 border border-slate-200 text-xs font-semibold text-slate-400 cursor-not-allowed";
      actionTitle = ' title="' + escapeHtml(reason) + '"';
    }
    // Column order (2026-07-23 round-5): Source | Sender | Body |
    // Received | Status | Actions. Body is the longest column
    // (no truncate) so the operator can read the message without
    // hover-to-reveal. Source / Sender / Received / Status /
    // Actions are minimal fixed width; Body takes the remaining
    // space. Status sits second from the right (between Received
    // and Actions) so the operator reads the suppression state
    // immediately to the left of the action button it gates.
    // Media column added 2026-07-23 between Body and Received —
    // mirrors the /messages archive column.
    tr.innerHTML =
      '<td class="px-4 py-3 whitespace-nowrap w-20">' +
      sourceBadge(source) +
      "</td>" +
      '<td class="px-4 py-3 w-44">' +
      senderCell(sender, senderName) +
      "</td>" +
      '<td class="px-4 py-3"><div class="text-slate-700 break-words whitespace-pre-wrap">' +
      escapeHtml(body) +
      mediaBadge +
      "</div></td>" +
      '<td class="px-4 py-3 w-32">' +
      mediaCell(media) +
      "</td>" +
      '<td class="px-4 py-3 text-xs text-slate-500 whitespace-nowrap w-32 text-right">' +
      escapeHtml(fmtDate(receivedAt)) +
      "</td>" +
      '<td class="px-4 py-3 whitespace-nowrap w-32 text-xs text-center">' +
      statusCell(suppressed, rules) +
      "</td>" +
      '<td class="px-4 py-3 text-center whitespace-nowrap w-28">' +
      '<button data-suppress-id="' +
      escapeHtml(id) +
      '" data-suppress-target="' +
      actionState +
      '" class="' + actionClasses + '"' +
      actionAttrs +
      actionTitle +
      ">" +
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
        '<td colspan="7" class="px-4 py-8 text-center text-slate-400">No messages yet.</td>';
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
    // Coerce the result of `App.getMessages(...)` into a real JS Array
    // of PLAIN objects (not PyProxies). The previous version did
    // `Array.from(rows)` which lifts the outer list proxy but leaves
    // each `msg` as a borrowed PyProxy tied to the iterator lifetime.
    // When the operator's next onChange fires, the iterator is exhausted
    // and `msg.id` / `msg.body` / etc. raise
    // "This borrowed proxy was automatically destroyed when an
    // iterator was exhausted." The fix is to deep-convert each row
    // here, while the iterator is still alive, into a plain JS object
    // with every field we need (id, sender, sender_name, body, source,
    // received_at, suppressed, rules, media) extracted eagerly. Plain
    // JS objects don't carry a Python lifetime, so subsequent renders
    // (after another onChange tears down the previous list proxy) work
    // fine.
    const extracted = [];
    if (rows == null) {
      // No messages — leave `extracted` empty.
    } else if (Array.isArray(rows)) {
      for (let i = 0; i < rows.length; i++) {
        extracted.push(extractMsg(rows[i]));
      }
    } else if (typeof rows.length === "number") {
      // PyProxy list — iterate by index while it's still alive.
      try {
        for (let i = 0; i < rows.length; i++) {
          extracted.push(extractMsg(rows[i]));
        }
      } catch (_) {
        // iterator exhausted; return what we got
      }
    }
    // "Hide suppressed" toggle (2026-07-23 round-5). The page
    // header carries a checkbox; when on, we filter out any row
    // whose `suppressed` is true BEFORE the pagination layer
    // sees the array. The page-info line below still reports
    // the post-filter count so the operator knows how many
    // records are visible after applying the filter.
    if (hideSuppressed) {
      return extracted.filter(function (m) { return !m.suppressed; });
    }
    return extracted;
  }

  function extractMsg(raw) {
    // Deep-extract the fields `renderRow` reads from a Python
    // MessageView proxy. Defensive: any field that fails to
    // read returns a sensible default. The whole point is to
    // produce a plain object whose property accesses don't
    // require a live Python proxy.
    //
    // PyProxy access contract (2026-07-23 round-5 hotfix):
    //   - `get_messages(...)` returns MessageView instances
    //     (Python class, not dict). Their `body` / `sender` /
    //     `received_at` / `source` / `suppressed` / etc. live
    //     on `self.message.*` (Message) or on `self.*`
    //     (MessageView), but `to_dict()` flattens them into a
    //     plain Python dict — the canonical wire shape.
    //   - Attribute access (`proxy.body`) returns undefined for
    //     fields that exist on `MessageView.to_dict()` but not on
    //     the instance itself (the table was rendering empty
    //     cells for ~16 fields because of this).
    //   - Once you call `proxy.to_dict()`, the returned PyProxy
    //     is a dict. Dict proxies do NOT expose keys as
    //     enumerable JS properties (so `for-in`, `JSON.stringify`
    //     and `proxy[key]` all return empty / undefined), but
    //     they DO have `.get(key)` from Mapping. Always read
    //     dict values via `.get(key)`.
    if (raw == null) return null;
    let dict = null;
    try {
      if (typeof raw.to_dict === "function") {
        dict = raw.to_dict();
      }
    } catch (_) { dict = null; }
    // `dictGet` returns the value for `key` from a dict proxy,
    // falling back to attribute access if `dict` is not a
    // Mapping (covers the case where the proxy is a plain
    // object instead of a dict).
    function dictGet(d, key) {
      if (d == null) return undefined;
      try {
        if (typeof d.get === "function") {
          const v = d.get(key);
          if (v !== undefined && v !== null) return v;
        }
      } catch (_) { /* fall through */ }
      try { return d[key]; } catch (_) { return undefined; }
    }
    function field(name) { return dictGet(dict, name); }
    const rules = (function () {
      const r = field("rules");
      if (!Array.isArray(r) && !(r && typeof r.length === "number")) return [];
      const out = [];
      try {
        for (let i = 0; i < r.length; i++) {
          const one = r[i];
          if (one == null) continue;
          out.push({
            type: dictGet(one, "type"),
            pattern: dictGet(one, "pattern"),
            action: dictGet(one, "action"),
            status: dictGet(one, "status"),
          });
        }
      } catch (_) { /* iterator exhausted; return what we got */ }
      return out;
    })();
    const media = (function () {
      const m = field("media");
      if (!Array.isArray(m) && !(m && typeof m.length === "number")) return [];
      const out = [];
      try {
        for (let i = 0; i < m.length; i++) {
          const one = m[i];
          if (one == null) continue;
          out.push({
            type: dictGet(one, "type"),
            url: dictGet(one, "url"),
          });
        }
      } catch (_) { /* iterator exhausted */ }
      return out;
    })();
    return {
      id: field("id") || "",
      sender: field("sender") || "",
      sender_name: field("sender_name") || "",
      body: field("body") || "",
      source: field("source") || "",
      received_at: field("received_at") || field("receivedAt") || "",
      suppressed: !!field("suppressed"),
      rules: rules,
      media: media,
    };
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
  // "Hide suppressed" toggle (2026-07-23 round-5). When the
  // operator flips the checkbox, re-apply the filter and reset
  // the page cursor to 0 so the new top of the table is visible.
  if (hideSuppressedEl) {
    hideSuppressedEl.addEventListener("change", function () {
      hideSuppressed = !!hideSuppressedEl.checked;
      currentPage = 0;
      reload();
    });
  }

  // §7.8: suppress / unsuppress actions are single-flight per row
  // and never reload the document. The button is disabled during
  // the in-flight POST so a double-click can't double-publish. The
  // Unsuppress button is ALSO disabled at render time when the row
  // is suppressed for a non-`type:message` reason (see
  // `renderRow` above); the click handler is a defensive no-op
  // for already-disabled buttons because the `disabled` attribute
  // suppresses the event.
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