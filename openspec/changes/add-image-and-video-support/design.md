## Context

The Twilio inbound webhook at `heart-message-manager/main.py:api_messages()` consumes the form-encoded POST and currently reads only `From` and `Body`. Twilio's MMS webhooks carry `NumMedia`, `MediaContentType0..N`, `MediaUrl0..N` — the existing handler silently drops them. Twilio's `MediaUrl*` is a URL on `api.twilio.com` that expires (Twilio documents a ~30-day retention, but the exact window is enforced server-side and there is no SLA on it — in practice we've seen 410 GONE within days of send). The existing S3 backup (`heart-message-manager/s3.py:log_message`) is the canonical persistence path for inbound messages; media follows the same fire-and-forget-before-response pattern.

The Pi device renders backgrounds through `lib_shared/patterns/*` Effect subclasses, instantiated by `lib_shared/effects_factory.py:make_effect_class` and arranged by `lib_shared/effects_coordinator.py:EffectsCoordinator` from the `EffectsSettings.effects` list in `SignConfig`. The coordinator is the lifecycle owner (intro → out → in → hold → text_out → background); the active "background" comes from `self.effects[self.idx]`. There's no path today for a single inbound message to install a *temporary* override of that background.

Two effect classes can render real media:
- `lib_shared/patterns/png_display.py:PngDisplay` — palette-indexed, takes a directory of PNGs and crossfades between them. Default is `<repo>/design/pngs/`.
- `lib_shared/patterns/video_display.py:VideoDisplay` — full-RGB frame blit via OpenCV, takes a single video path. Already supports `.mp4 / .mov / .avi / .mkv / .webm / .gif`.

Both are disabled by default in `lib_shared/models.py:_DEFAULT_EFFECTS_LIST_FULL` because they expect operator-supplied paths, not per-message URLs.

## Goals / Non-Goals

**Goals:**

1. Inbound MMS attachments (image/* + video/*) become background media for the message's display cycle, end-to-end from Twilio to panel.
2. The text body still scrolls; the media just replaces the background during the message's hold window.
3. The classic behavior (rotation between fire/flame/sky/etc. effects on SMS-only messages) is preserved verbatim.
4. S3 retention strategy mirrors the existing `messages/{year}-{month}/` keys — same disaster-recovery story, same S3 layout.
5. Backward-compatible wire: existing 4-field `Message` shapes round-trip; media field defaults to `[]`.
6. PngDisplay becomes ImageDisplay with PIL-driven format support (PNG/JPEG/GIF/WebP) — same module, same palette rendering, expanded glob.

**Non-Goals:**

1. Animated GIF playback beyond the first frame (PIL collapses animations when calling `.convert("RGB")`). A follow-up change can wire `imageio` if needed; this change keeps GIF as a static single-frame.
2. Audio attachments. Twilio supports inbound voice recordings (`audio/*`), but the LED panel has no audio path and the device has no speaker. If a future change needs audio, the storage layer here would carry it through with a `media/audio/{YYYY-MM}/` prefix.
3. Multi-message deduplication of media. If two SMS arrive with the same attachment URL, both copies land in S3 (cheap; S3 storage is the durable side-channel).
4. Authenticated outbound delivery receipts from the panel. This is a "render this URL" path, not a "verify which URLs the user saw" path.
5. Pre-signed S3 URLs. Rejected — the Flask proxy is durable and auth-aware.
6. Live transcoding. We assume Twilio's MMS handover is in a renderable format (PIL for images, OpenCV for video). A real-world codec mismatch (e.g., an inbound `.avi` with a non-standard codec) falls back to a black panel + INFO log.

## Decisions

### D1 — Single S3 namespace, two prefixes

- **Decision:** Add two prefixes — `media/images/{YYYY-MM}/...` and `media/videos/{YYYY-MM}/...` — alongside the existing `messages/{YYYY-MM}/...` and `config/{YYYY-MM}/...`.
- **Rationale:** Matches the issue's request ("Add /videos and /images directories and follow the same YYYY-MM folder strategy"). Per-content-type prefixes keep `s3 ls` over a single bucket organized, and the date-based month folders cap the blast radius if a list call goes wrong.
- **Alternatives:**
  - **Flat prefix `media/<sha>.<ext>`**: simpler, no temporal grouping. Rejected because the existing `messages/{YYYY-MM}/` already establishes the temporal pattern — operators reading `s3 ls` find new media near new messages.
  - **Separate bucket**: would require a new bucket, new env vars, new IAM scope. The issue stays inside the existing bucket by spec.

### D2 — Wire `url` field is an S3 *key*, not an HTTPS URL (signed URL generated on demand at fetch time)

- **Decision:** `Message.media[*].url` stores the S3 key (`media/images/2025-12/foo.jpg`), not the literal `s3://...` and not a pre-signed HTTPS URL.
- **Rationale:** Keys never expire. The Flask endpoint at `GET /api/media/<path:key>` calls `s3.signed_media_url(key)` to mint a fresh 1-hour signed URL on every fetch and returns it as a `302 Location` header (see D3). The Pi constructs `cfg.API_BASE_URL + "/api/media/" + key` and hits that with the existing API key header; Flask redirects it to S3. The browser does the same. The TTL is invisible to the wire.
- **Alternatives:**
  - **Pre-signed S3 URLs (default 1h TTL) baked into the wire**: cheap to implement but every caller becomes responsible for URL refresh, and the wire becomes a snapshot of the moment the message was published, not a stable reference. Rejected (see D3).
  - **`s3://` URIs**: resolvable from within AWS but the Pi is on a residential Wi-Fi network with no VPC endpoint; an `s3://` URL wouldn't help.

### D3 — Flask returns 302 to a freshly-signed S3 URL (no streaming proxy)

- **Decision:** `GET /api/media/<path:s3_key>` authenticates the caller (`api_login_required`), then calls `s3.signed_media_url(s3_key)` which returns `boto3.client("s3").generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)`. Flask responds with `302 Found` and a `Location:` header pointing at the signed URL. The Pi (and browser) follows the redirect — bytes come directly from S3, never through Flask.
- **Rationale:** Flask already has boto3 + AWS credentials. The auth boundary stays on Flask (Pi/browser never see AWS creds and never need S3 SDK calls). Bytes go S3 → client directly, so Flask bandwidth / CPU is not on the media path. A 1-hour signed URL TTL is invisible to the wire — clients hold a logical `s3_key` (in `Message.media[*].url`) and re-fetch (getting a fresh signed URL each time) on every display.
- **Alternatives:**
  - **Streaming proxy (`Flask → S3 → Flask → client`)**: simplest from a client perspective but pushes every media byte through the Flask tier, doubling Heroku egress for no auth benefit (the signed URL carries S3-side auth in its query string). Rejected.
  - **Pre-bake signed URLs into `Message.media[*].url`**: puts TTL handling in every consumer; admin UI has to re-sign old messages; a Pi holding the URL > 1 hour eventually 403s. Rejected.
  - **Public-read bucket**: cheapest, but leaks the bucket's URL on every Twilio MMS. Operators want media private. Rejected.

### D4 — MediaCycler is per-message, not a new Effect in the rotation

- **Decision:** A `MediaCycler` Effect exists *only* in service of the current inbound message. When the coordinator's `get_display_message()` returns body X, the coordinator's `get_display_media(message_id)` returns X's media list; if non-empty, the coordinator swaps `self.current` to a fresh `MediaCycler(...)` for that cycle. When the cycle ends or `hold_seconds` elapses, the cycler drops back to `self.effects[self.idx]`.
- **Rationale:** The issue says "the background selection in EffectsCoordinator should use the provided media file(s)" — that's per-message, not a permanent entry in the rotation. A new entry in `_DEFAULT_EFFECTS_LIST_FULL` would force the operator to remember to keep it disabled for SMS-only messages.
- **Alternatives:**
  - **Permanent "MediaDisplay" rotation entry**: would always be ready, but most of the time it'd have nothing to display (the default `media: []` message). Confusing UX.
  - **Mutation of rotation list at runtime**: would race with config writes arriving over MQTT and put non-determinism into the rotation's index advance.

### D5 — Cycle window = `max(10s, media_duration)`, cut off at `hold_seconds`

- **Decision:** The cycler renders each item for `max(10 s, source_duration)`. If the sum exceeds `hold_seconds`, the cycler drops mid-message back to rotation. Cut-off is preferred over extending `hold_seconds`.
- **Rationale:** The issue's "either extend hold_seconds, or just cut off" choice. Cut-off keeps timing predictable: a 15 s `hold_seconds` config behaves the same whether the message has 1 photo or 12. Extending would mean a 12-photo message could hold the panel for 2+ minutes regardless of what the operator configured.
- **Alternative:** Extend `hold_seconds` to fit all media. Rejected because the operator-set `hold_seconds` is a deliberate pacing choice.

### D6 — `PngDisplay` → `ImageDisplay` rename (internal-only, removed from effects registry)

- **Decision:** Rename `lib_shared/patterns/png_display.py` → `image_display.py` and the class `PngDisplay` → `ImageDisplay`, expanding the load path to PNG/JPEG/GIF/WebP. The renamed class is **not** an entry in `lib_shared/config/effects_settings.json` anymore — the rotation's job is to fill idle time between messages, and the operator-curated `design/pngs/`/`design/videos/` path it used to provide is superseded by the MMS flow. `ImageDisplay` and `VideoDisplay` exist as inner renderers consumed by `MediaCycler` (Design D7) via direct Python imports; the effects registry carries only the 5 non-media effects (Hyperspace, Honeycomb, Flame, Fireworks, NightSky). `lib_shared/effects_loader.py` requires no change — it never sees the renamed classes. Operator overrides (`config_overrides/effects_settings.json`) that still carry a `PngDisplay` or `ImageDisplay` entry hit the loader's existing "unknown effect name" branch: `make_effect_class(...)` returns `None`, logs a WARNING, and `build_effects()` silently skips the entry. No crash, no broken sign.
- **Rationale:** The class is no longer PNG-specific (it supports JPEG/GIF/WebP). Keeping the old name as the canonical would mislead future contributors. Removing it from the registry makes the dependency direction explicit: `MediaCycler` → `ImageDisplay`/`VideoDisplay` (direct import), and the rotation's effects registry no longer has any surface coupling to media. The curated-content rotation use case (operator drops files into `design/pngs/`, sign cycles them as background) is replaced by MMS — send yourself an MMS to get an image onto the sign.
- **Alternatives:**
  - **Keep `ImageDisplay` in the registry, enabled by default.** Rejected because the rotation's only legitimate media use case is now per-message, via `MediaCycler`. Carrying a rotation entry that overlaps with `MediaCycler`'s role adds operator config surface for no benefit.
  - **Keep `ImageDisplay` in the registry, disabled by default (operator opt-in).** Rejected because disabled-by-default + opt-in still leaves the entry as a first-class concept that operators have to think about; the curated-content path is dead anyway.

### D7 — `MediaCycler` is one Effect class, internally polymorphic; imports inner renderers directly

- **Decision:** `MediaCycler` accepts the media list at construction time and, per item, dispatches by `mime` (`image/*` → `ImageDisplay`, `video/*` → `VideoDisplay`). The inner renderer classes are imported directly: `from lib_shared.patterns.image_display import ImageDisplay` and `from lib_shared.patterns.video_display import VideoDisplay`. They are **not** looked up via `effects_loader.make_effect_class(...)` — those classes are no longer in the effects registry (D6). Same Effect interface (`tick`, `render`, `set_brightness`) on the cycler.
- **Rationale:** Keeps the coordinator's "swap the active Effect" interaction shape identical. Direct import is the natural choice now that the renamed classes are internal implementation details rather than registry-listed effects. The mime-type dispatch is the only polymorphism the cycler needs.
- **Alternative:** Look the inner renderers up via `make_effect_class("ImageDisplay")` / `make_effect_class("VideoDisplay")`. Rejected because those names are not in the registry anymore; the lookup would return `None` and force the cycler to fall back to direct imports anyway. Direct import is simpler and explicit.
- **Alternative:** Two separate cycler classes (`ImageCycler` + `VideoCycler`). Adds a coordinator decision per cycle with no benefit — the cycler has the same shape regardless.

### D8 — MMS auth uses Twilio Basic, independent of webhook signature

- **Decision:** `log_media` downloads from `MediaUrl*` using HTTP Basic Auth constructed from `cfg.TWILIO_AUTH_TOKEN` and the inbound `MessageSid`'s `AccountSid`. Independent of the request's X-Twilio-Signature.
- **Rationale:** Twilio's `MediaUrl*` returns binary content authenticated via Basic Auth; the webhook signature only covers the form fields of the inbound request itself, not subsequent URL fetches. We use the same token for the webhook signature validation AND for Basic-auth'ing the URL fetch — one credential, two uses.
- **Alternative:** Have Twilio proxy through a webhook relay. Out of scope — adds a new external dependency.

### D9 — `ImageDisplay` keeps palette-based rendering, drops the alpha-channel trick

- **Decision:** The existing PNG path uses `img.getchannel("A")` as a mask against a white-on-black canvas. JPEG has no alpha, GIF and WebP vary. Change the loader to `convert("RGB")` (drops alpha), and accept that transparent-background images render against the canvas's black background. Update the alpha-mask step accordingly — for images with alpha, use the alpha as a white-on-black mask the same way the PNG path did, but only when alpha exists.
- **Rationale:** Most PNGs shipped in design/pngs/ are black-on-transparent line art. Dropping the alpha support entirely would degrade the existing design content. Conditional mask (alpha when present, ignore when absent) preserves the old look while adding JPEG/GIF/WebP.
- **Alternative:** Always white-on-black (drop alpha support entirely). Rejected because it changes how the curated `design/pngs/` content renders.

### D10 — Empty body with media is accepted; empty body with no media still returns 204

- **Decision:** The existing `_process_inbound_message` 204 gate fires when `Body` is empty. Change it: accept the message iff `body` is non-empty OR `NumMedia > 0`. A media-only MMS persists (text="", full media list), publishes over MQTT, and the coordinator routes it to the `MediaCycler` with an empty scroller. An empty body + `NumMedia=0` (or absent) still returns 204 — there is nothing to display, the same as today.
- **Rationale:** MMS supports a media-only payload (e.g., a photo with no caption). The operator expects it to render the photo on the sign. Dropping the message at the webhook gate is a regression relative to the SMS-only path's "empty body → no-op" because the MMS has a non-empty media list — there *is* something to display. The existing `EffectsCoordinator` already has the `text=""` branch in the `out → in` transition (`scroller.set_text("", display.width)`, `showing_text=False`, mode is `background` after the fade-in instead of `hold`), so the state machine needs no change.
- **Alternative:** Reject the message and return a 4xx to Twilio. Rejected because it would require Twilio to retry (and it won't — failed deliveries are lost) and it gives the operator no recovery path.
- **Alternative:** Always accept and render. Rejected because an SMS with `Body=""` and no media (a stray empty form post, e.g. from Twilio's webhooks during transient carrier issues) would publish a no-op Message over MQTT and waste a rotation slot.

### D11 — PR-53 wire-strip is orthogonal to per-message media

- **Decision:** PR-53's wire-strip behavior (when an operator override is active on the Pi, the entire `effects_settings` block — effects list + pacing — is dropped before `update_from_dict` runs; `text_settings`, `filters`, `senders`, `sign`, `timezone` still come from the wire) has zero interaction with `MediaCycler`.
- **Rationale:** `MediaCycler` is constructed per-message at the `out → in` transition from `Message.media` (a Message-level field on the wire, not a `SignConfig.effects_settings` field). The wire-strip affects `SignConfig`, not `Message`. Operators using the override still get full media-driven backgrounds for messages with media; they just can't tune `fade_seconds`/`hold_seconds`/etc. via the admin UI (those come from the override JSON instead).
- **Alternative:** Honor wire `effects_settings` even with override active when the current message has media. Rejected because the wire-strip is intentional — the override is the operator's signal that they want full local control. Mixing the two paths would make the override's scope ambiguous.

### D12 — `MediaCycler` removes bad-codec items; empty list falls back to rotation

- **Decision:** When the inner renderer (`VideoDisplay` for `video/*`, `ImageDisplay` for `image/*`) raises or signals a decode failure on the current item — codec mismatch, corrupt bytes, OpenCV can't read the frame, PIL reports a decode error, etc. — `MediaCycler` removes that item from its in-memory list, logs a WARNING (`"MediaCycler: dropping item %r due to decode failure: %s"`), and advances to the next. If the list becomes empty after removal, the cycler yields back to `self.effects[self.idx]` (the rotation) on the next fade — same path as D5's `hold_seconds` cutoff.
- **Rationale:** A single bad item shouldn't blank the panel — the cycler should advance to the next good item. If every attachment Twilio delivered is undecodable (rare but possible: HEIC with no PIL plugin, MOV with a non-standard codec), the rotation's default effects (Flame, NightSky, Fireworks, etc.) keep the sign alive. This matches the existing rotation's "skip silently broken effects" behavior at `lib_shared/effects_coordinator.py:build_effects()`.
- **Alternative:** Render the failed item as a black frame + WARNING, advance on `hold_seconds`. Rejected because a series of bad attachments would yield a black panel for `hold_seconds × count` — a worse UX than falling back to the rotation's standard effects.
- **Alternative:** Bail the entire message on the first failure. Rejected because the user sent multiple attachments and at least some are likely fine; the rotation fallback only kicks in if ALL fail.

### D13 — Async media upload: respond to Twilio immediately, publish MQTT after uploads complete

- **Decision:** `_process_inbound_message` returns the 200/TwiML response to Twilio **before** any media download. A daemon `threading.Thread` (or `concurrent.futures.ThreadPoolExecutor` over the media list for parallel downloads) then:
  1. Downloads each `MediaUrl*` via Twilio Basic Auth.
  2. Uploads each to S3 under the correct prefix.
  3. Builds the `media: list[{type, url}]` list (dropped items where download or upload failed).
  4. Persists the `Message` (text + completed media list) to S3 + SQLite.
  5. Publishes the `MessageEnvelope` over MQTT exactly once.
- A `MessageSid`-keyed dedupe guard (in-process dict or short-lived S3 marker) prevents double-processing if Twilio retries the webhook while the background thread is still running.
- **Rationale:** Eliminates R6 (the race between webhook return and S3 write — there is no race because the MQTT publish happens *after* media is durably in S3). Preserves Twilio's retry budget (fast response). Trade-off: the Pi sees the message after a brief delay (typically <2s for parallel uploads of typical MMS); the text-only path is unchanged and unaffected.
- **Alternative:** Synchronous upload before TwiML response. Rejected because a slow S3 put could blow the 15-second Twilio response window (R2), and the synchronous path is incompatible with the cleanest R6 fix.
- **Alternative:** Publish a text-only `MessageEnvelope` immediately, then a second `type="message-update"` envelope when media lands. Rejected because it doubles the wire-shape surface and requires Pi-side two-phase handling for a benefit (earlier Pi display of just the text) that doesn't matter at <2s latency.

## Risks / Trade-offs

- **[R1] Twilio Basic Auth lifetime:** if `TWILIO_AUTH_TOKEN` rotates, the in-flight `MediaUrl*` downloads will 401 mid-cycle. → Mitigation: wrap `log_media` in try/except for `boto3`/HTTP errors; log WARNING and continue with empty media list. The text never depends on media succeeding.
- **[R2] Media attachment size blow-up:** Twilio's webhooks only contain URLs, not the bytes themselves — a 50 MB MP4 attachment means a 50 MB download + upload on our side. → Resolved by D13: Twilio gets its 200/TwiML response immediately (well under the 15 s budget); the downloads happen in a background thread after the response is sent. Twilio's retry behavior on the webhook itself is unaffected because we always ack fast.
- **[R3] GIF animation:** `Pillow`'s `.convert("RGB")` collapses animations. → Mitigation: document this in the design; the user can use a short looping MP4 for animations. Documented in `image_display.py` docstring.
- **[R4] _(Removed — D6 drops the deprecated-alias scheme entirely; no compatibility shim lifespan to track.)_
- **[R5] Pi memory pressure on large videos:** a 50 MB MP4 in `VideoDisplay`'s OpenCV `VideoCapture` lands in Pi RAM via the decoder's intermediate buffers. → Mitigation: no upload-time size cap (we capture everything Twilio sends — the sender expects their photo/video to appear). `VideoDisplay` reads frame-by-frame via `cv2.VideoCapture.grab()` + `retrieve()`, so per-frame memory is bounded by frame dimensions (~6 MB for 1080p) regardless of total video size. For pathological cases (very-high-bitrate H.265, 4K source), `MediaCycler` (D12) drops the offending item with a WARNING and advances to the next — the rotation's default effects (Flame, NightSky, Fireworks) take over if every item is over-budget. Pi can also truncate via OpenCV re-encode in a future enhancement; not required for v1.
- **[R6] Race: webhook returns before media is in S3:** → Resolved by D13. The MQTT publish happens *after* the background thread completes all uploads; there is no window in which a `MessageEnvelope` exists with media URLs that aren't yet durable in S3.
- **[R7] S3 cost from un-pruned media:** media is far more storage-bulky than text messages. The existing `_prune_config_snapshots` keeps the last 10; we need a similar policy for media. → Mitigation: this change does NOT auto-prune; the operator manually rotates or sets an S3 lifecycle rule. Document in the design.
- **[R8] Browser preview's PyScript runtime + image fetch:** the preview runs in WASM. Fetching 20 images through `/api/media/<key>` is fine for an admin page but adds load to Flask. → Mitigation: the preview uses a single image at a time (the active `MediaCycler` item). No change to the polling cadence.

## Migration Plan

**Deploy order (no DB schema, all additive):**

1. **Merge PR.** No migration of existing data. SQLite, S3, MQTT are all unaffected for SMS-only messages.
2. **Server restart.** Flask picks up the new `_process_inbound_message` and the `/api/media/<path:key>` route. Pre-existing rotations of messages don't have a `media` field — the `from_dict` default of `[]` handles them.
3. **Pi reboot.** `EffectsCoordinator` picks up the new MediaCycler path. Rotation continues to default to ImageDisplay enabled (and PngDisplay effectively replaced) instead of disabled.
4. **Operator verification:** send an SMS with an image, watch it appear in `/messages` with thumbnails, then watch the sign render it. Cut-off at `hold_seconds` is visible if the image would have run longer than the configured `hold_seconds`.

**Rollback:**

- Revert the merge commit; restart Flask; reboot Pi. S3 `media/` objects remain (just unused); the operator can clean up with a bucket-wide lifecycle rule or `aws s3 rm --recursive`.
- The `MediaCycler`'s effect class is only constructed when a message has media — even with the merge reverted, an SMS-only message still walks the existing 5-effect rotation.

## Open Questions

1. **Twilio webhook response timing — sync or async media upload?** Resolved by D13: async. Twilio gets 200/TwiML immediately; uploads run in a background thread; MQTT publishes after uploads complete. Eliminates R6.
2. **Should `MediaCycler` advance the message `hold_seconds` clock at first display, or at "natural media end"?** Current plan: hold clock starts at the `out → in` transition (existing behavior). The cycler shares the hold window with the scroller.
3. **Animated GIF support?** Out of scope for this PR. PIL collapses multi-frame GIFs to single-frame. A follow-up could swap PIL for `imageio` (Pyodide-compatible) and add a frame-tick path.
4. **Audio attachments?** Out of scope. Storage layer (`media/audio/{YYYY-MM}/`) lands here as a no-op; the device would need a future audio path to consume them.
5. **Pre-cache media at message-receive time.** Today the Pi (and browser) fetches each media item via `GET /api/media/<key>` → 302 → S3 on first render. If real-world p95 latency or S3 egress is unacceptable, a follow-up can pre-populate the Pi's media cache immediately after `s3.log_media(...)` succeeds: stream the bytes to `/var/cache/lindsay-50/<sha256(key)>.<ext>` and have `MediaCycler` look there first before falling back to the Flask redirect. The wire shape (`Message.media[*].url` as an S3 key) doesn't change. Tracked as a likely follow-up, not in scope for this PR.
