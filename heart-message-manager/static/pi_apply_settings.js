// pi_apply_settings.js — Apply button + click-to-edit for the Target Pi
// version input on the Settings page Pi Upgrade Control card (issue #51
// follow-up).
//
// Two responsibilities, both short-circuited when the relevant DOM hooks
// are absent:
//
//   - Click-to-edit: when the operator focuses the input and its current
//     value matches the Flask-version placeholder (i.e. the field is
//     showing "inherit Flask" because the persisted target is empty),
//     clear it so the operator can type a specific SHA. The placeholder
//     text is read from `data-flask-version-placeholder` on the input —
//     the same string the Jinja side uses as the HTML `placeholder`
//     attribute, so the operator gets a single visual cue both before
//     and after focus.
//
//   - Apply button: track dirty state against `data-saved-value` (set
//     by the template at render time). The button is disabled by default
//     and enabled when the input differs from the saved value. On click
//     it submits the surrounding `<form method="POST">` — Flask's
//     existing /settings handler does the persistence + config-envelope
//     publish + check-for-update nudge. No new endpoint, no new envelope
//     type. JS just toggles the button state and listens for the input.

(function () {
  "use strict";

  const root = document.querySelector("[data-upgrade-settings-field]");
  if (!root) return;

  const input = root.querySelector("[data-upgrade-target-input]");
  const applyBtn = root.querySelector("[data-upgrade-apply]");
  if (!input || !applyBtn) return;

  // data-saved-value is the persisted target_version (post-construction).
  // Empty string is the legit "inherit Flask version" state — no
  // normalization to None needed.
  const savedValue = input.getAttribute("data-saved-value") || "";
  // data-flask-version-placeholder mirrors the HTML `placeholder`
  // attribute so focus-clearing matches exactly what the operator sees.
  const flaskPlaceholder = (
    input.getAttribute("data-flask-version-placeholder") || ""
  ).trim();

  function currentIsDirty() {
    return (input.value || "").trim() !== savedValue;
  }

  function refreshApply() {
    applyBtn.disabled = !currentIsDirty();
  }

  // Initial state — the HTML `disabled` attribute covers the "saved
  // value matches current" case, but reset here so a future render path
  // that mutates `savedValue` programmatically stays consistent.
  refreshApply();

  // Track dirty state on every keystroke. `input` covers typing and
  // paste (the two ways the value actually changes); `change` would miss
  // typing in real time and `blur` is too late (the operator would see
  // the disabled-button state for the duration of their typing).
  input.addEventListener("input", refreshApply);

  // Click-to-edit on focus: if the field shows the Flask placeholder
  // (i.e. the persisted value is empty AND the field has been left in
  // a "placeholder visible" state — e.g. after page load, or after the
  // operator tabbed away without typing), clear it on focus so they
  // can type a specific value. We use `value === placeholder` (not
  // `value === empty`) so we only clear when the input is actually
  // showing the Flask-version hint, not any time the operator typed
  // empty + they tab away + come back.
  //
  // Spec: "if it hasn't been changed since being saved, clicking on it
  // to change it should clear the text." When `savedValue === ""`, the
  // placeholder IS what the operator sees — clearing on focus is the
  // intended behavior.
  input.addEventListener("focus", () => {
    const placeholder = (input.placeholder || "").trim();
    const current = (input.value || "").trim();
    if (flaskPlaceholder && current === placeholder) {
      input.value = "";
      refreshApply();
    }
  });

  // Apply click — submit the surrounding form. The form may not be a
  // direct parent of `[data-upgrade-settings-field]` (Jinja includes
  // this card in an outer <form method="POST">); walk up to find it.
  applyBtn.addEventListener("click", () => {
    if (applyBtn.disabled) return;
    const form = input.closest("form");
    if (!form) {
      // No enclosing form (Settings page without a form wrapper).
      // Fall back to clicking the form's submit button if one is in
      // scope; otherwise log so a future regression is debuggable.
      const submit = root.querySelector(
        'button[type="submit"]'
      );
      if (submit) submit.click();
      return;
    }
    // requestSubmit() triggers form validation + the submit event
    // with the right submitter (the Apply button). Falling back to
    // `.submit()` works in browsers without requestSubmit, but
    // requestSubmit is preferred because it preserves the submitter
    // semantic (the form-submit action sees "Apply" as the source).
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit(applyBtn);
    } else {
      form.submit();
    }
  });
})();
