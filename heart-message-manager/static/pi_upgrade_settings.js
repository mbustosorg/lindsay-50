// pi_upgrade_settings.js — Settings-page Pi Upgrade Control section (issue #51).
//
// Wiring for `[data-upgrade-settings-field]`:
//
//   - Three command buttons (`data-action="force-upgrade"`,
//     "restart", "shutdown"): each gates with `confirm()` (a small
//     operator-actionable signal — these are NOT reversible from the UI),
//     then POSTs to `/api/sign/commands/<action>` with the
//     X-API-Key header. On 202 the user sees a green confirmation
//     toast; on 4xx/5xx they see the JSON error.
//
// The module is short-circuited (no DOM writes, no fetch) when the
// `[data-upgrade-settings-field]` element is absent — safe to include
// on every page via base.html.

(function () {
  "use strict";

  const root = document.querySelector("[data-upgrade-settings-field]");
  if (!root) return;

  const apiKey =
    (window.APP_CONFIG &&
      window.APP_CONFIG.auth &&
      window.APP_CONFIG.auth.API_SECRET_KEY) ||
    "";

  const COMMANDS = {
    "force-upgrade": {
      label: "force-upgrade",
      confirm: "Force the Pi to upgrade to its resolved target now?",
    },
    restart: {
      label: "restart",
      confirm: "Restart the Pi? This will interrupt the display for ~30s.",
    },
    shutdown: {
      label: "shutdown",
      confirm:
        "Shut down the Pi? You will need to power-cycle it manually to bring it back up.",
    },
  };

  function ensureToastContainer() {
    let host = document.getElementById("upgrade-toast-host");
    if (host) return host;
    host = document.createElement("div");
    host.id = "upgrade-toast-host";
    host.style.cssText =
      "position:fixed;right:24px;bottom:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:400px;";
    document.body.appendChild(host);
    return host;
  }

  function showToast(kind, text) {
    const host = ensureToastContainer();
    const colors =
      kind === "ok"
        ? { bg: "#dcfce7", br: "#86efac", fg: "#166534" }
        : kind === "warn"
          ? { bg: "#fef3c7", br: "#fcd34d", fg: "#92400e" }
          : { bg: "#fee2e2", br: "#fca5a5", fg: "#991b1b" };
    const el = document.createElement("div");
    el.style.cssText =
      `padding:12px 16px;border-radius:12px;background:${colors.bg};border:1px solid ${colors.br};color:${colors.fg};font-weight:600;font-size:14px;box-shadow:0 4px 6px rgba(0,0,0,0.05);`;
    el.textContent = text;
    host.appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity 600ms";
      el.style.opacity = "0";
      setTimeout(() => host.removeChild(el), 700);
    }, 4000);
  }

  // ---- Command buttons ----

  root.querySelectorAll("[data-action]").forEach((btn) => {
    const action = btn.getAttribute("data-action");
    const spec = COMMANDS[action];
    if (!spec) return;

    btn.addEventListener("click", async () => {
      if (!apiKey) {
        showToast("err", "Missing API key (window.APP_CONFIG.auth.API_SECRET_KEY is empty).");
        return;
      }
      if (!window.confirm(spec.confirm)) return;

      btn.disabled = true;
      const originalLabel = btn.textContent;
      btn.textContent = originalLabel + " …";
      try {
        const resp = await fetch(`/api/sign/commands/${action}`, {
          method: "POST",
          headers: { "X-API-Key": apiKey },
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 202) {
          showToast(
            "ok",
            `Published ${spec.label} (Pi will pick it up shortly).`
          );
        } else if (resp.status === 401) {
          showToast("err", "401 — API key rejected.");
        } else if (resp.status === 404) {
          showToast("err", `404 — unknown action '${action}'.`);
        } else {
          const reason =
            (data && (data.error || JSON.stringify(data))) ||
            `HTTP ${resp.status}`;
          showToast("err", `Failed: ${reason}`);
        }
      } catch (err) {
        showToast("err", `Network error: ${err.message || err}`);
      } finally {
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
    });
  });
})();
