// Standalone cache-clearing helper for the login page.
//
// The full `app.js` bootstrap (and the PyScript runtime it
// pulls in) is gated by `{% if current_user.is_authenticated %}`
// in `base.html`, so it never runs on the login page. But the
// login page still needs the same "wipe any prior user's cache
// in this tab before submitting credentials" guarantee — a
// browser crash, an interrupted logout navigation, or a stale
// tab left open across user changes would otherwise leave the
// previous session's MessageManager state in sessionStorage
// for the next user to inherit.
//
// Loading the full `app.js` here would drag in the PyScript
// runtime unnecessarily (and `app.js` doesn't define
// `App.clearMessageCache` until its IIFE has run, which is
// also gated on the IIFE executing in the right order).
// Instead, ship the minimum needed for this page: one helper
// on a tiny `App` namespace, mirrored from `app.js`'s
// `clearMessageCache`. Keep the two in sync — the rule
// "lindsay50:* keys are ours" is what matters, not the
// implementation.

(function () {
  "use strict";

  function clearMessageCache() {
    let wiped = 0;
    try {
      for (let i = sessionStorage.length - 1; i >= 0; i--) {
        const k = sessionStorage.key(i);
        if (k && k.indexOf("lindsay50:") === 0) {
          sessionStorage.removeItem(k);
          wiped += 1;
        }
      }
      console.info("[login] cleared message cache (keys wiped:", wiped, ")");
    } catch (e) {
      console.warn("[login] clearMessageCache failed:", e);
    }
  }

  window.App = { clearMessageCache };
})();