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

### D2 — Wire `url` field is an S3 *key*, not an HTTPS URL

- **Decision:** `Message.media[*].url` stores the S3 key (`media/images/2025-12/foo.jpg`), not the literal `s3://...` and not a pre-signed HTTPS URL.
- **Rationale:** Keys never expire. The Flask proxy translates key → bytes via `s3.proxy_media(key)`. The Pi constructs `cfg.API_BASE_URL + "/api/media/" + key` and hits that with the existing API key header. The browser does the same.
- **Alternatives:**
  - **Pre-signed S3 URLs (default 1h TTL)**: cheap to implement but every caller becomes responsible for URL refresh, and the wire becomes a snapshot of the moment the message was published, not a stable reference. Rejected.
  - **`s3://` URIs**: resolvable from within AWS but the Pi is on a residential Wi-Fi network with no VPC endpoint; an `s3://` URL wouldn't help.

### D3 — Flask proxy vs. pre-signed URLs vs. public bucket

- **Decision:** Authenticated Flask proxy `GET /api/media/<path:key>`.
- **Rationale:** Flask already has boto3 + AWS credentials. Pi needs network access to one URL only (Flask), not a regional S3 endpoint. Browser already fetches `/api/*` with X-API-Key. Symmetric auth path on both clients. No new env vars.
- **Alternatives:**
  - **Public-read bucket**: cheapest, but leaks the bucket's URL on every Twilio MMS. Operators want media private.
  - **Pre-signed URLs**: see D2.

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

### D6 — `PngDisplay` → `ImageDisplay` rename, with `PngDisplay` alias kept

- **Decision:** Rename the class and module. The `effects_factory.make_effect_class("PngDisplay")` lookup *keeps* working and returns the renamed class. The default `_DEFAULT_EFFECTS_LIST_FULL` flips `"PngDisplay": false` → `"ImageDisplay": true`.
- **Rationale:** The class is no longer PNG-specific (it supports JPEG/GIF/WebP). Keeping the old name would mislead future contributors. The factory alias is one line and prevents existing `SignConfig` payloads from silently disabling the renamed class.
- **Alternative:** Leave `PngDisplay` named as-is and add a new `ImageDisplay`. Rejected because the `image/jpeg` and `image/webp` support is intrinsic to PIL's `Image.open()` — there's no "rename" cost beyond the file rename and import paths.

### D7 — `MediaCycler` is one Effect class, internally polymorphic

- **Decision:** `MediaCycler` accepts the media list at construction time and constructs an `ImageDisplay` or `VideoDisplay` per item as needed. Same Effect interface (`tick`, `render`, `set_brightness`).
- **Rationale:** Keeps the coordinator's "swap the active Effect" interaction shape identical. The polymorphism is internal.
- **Alternative:** Two separate cycler classes (`ImageCycler` + `VideoCycler`). Adds a coordinator decision per cycle with no benefit — the cycler has the same shape regardless.

### D8 — MMS auth uses Twilio Basic, independent of webhook signature

- **Decision:** `log_media` downloads from `MediaUrl*` using HTTP Basic Auth constructed from `cfg.TWILIO_AUTH_TOKEN` and the inbound `MessageSid`'s `AccountSid`. Independent of the request's X-Twilio-Signature.
- **Rationale:** Twilio's `MediaUrl*` returns binary content authenticated via Basic Auth; the webhook signature only covers the form fields of the inbound request itself, not subsequent URL fetches. We use the same token for the webhook signature validation AND for Basic-auth'ing the URL fetch — one credential, two uses.
- **Alternative:** Have Twilio proxy through a webhook relay. Out of scope — adds a new external dependency.

### D9 — `ImageDisplay` keeps palette-based rendering, drops the alpha-channel trick

- **Decision:** The existing PNG path uses `img.getchannel("A")` as a mask against a white-on-black canvas. JPEG has no alpha, GIF and WebP vary. Change the loader to `convert("RGB")` (drops alpha), and accept that transparent-background images render against the canvas's black background. Update the alpha-mask step accordingly — for images with alpha, use the alpha as a white-on-black mask the same way the PNG path did, but only when alpha exists.
- **Rationale:** Most PNGs shipped in design/pngs/ are black-on-transparent line art. Dropping the alpha support entirely would degrade the existing design content. Conditional mask (alpha when present, ignore when absent) preserves the old look while adding JPEG/GIF/WebP.
- **Alternative:** Always white-on-black (drop alpha support entirely). Rejected because it changes how the curated `design/pngs/` content renders.

## Risks / Trade-offs

- **[R1] Twilio Basic Auth lifetime:** if `TWILIO_AUTH_TOKEN` rotates, the in-flight `MediaUrl*` downloads will 401 mid-cycle. → Mitigation: wrap `log_media` in try/except for `boto3`/HTTP errors; log WARNING and continue with empty media list. The text never depends on media succeeding.
- **[R2] Media attachment size blow-up:** a 50 MB MP4 attachment could exhaust the Flask process memory or push the Twilio webhook response window past Twilio's 15-second limit. → Mitigation: stream download to disk first (we already have `/tmp` for staging); cap Twilio pull with a 10 s streaming timeout via `requests` `stream=True` + `iter_content(chunk_size=64KB)`; exceed-timeout → drop media, log, ship text. The Twilio webhook's 15 s response budget is on the *response*, not on the upload — Twilio accepts the form, then we have 15 s to construct the TwiML response. We can publish the text-only TwiML response first, do the S3 upload in a background thread, and update the wire-shape MQ envelope later. (See `[open question]` below.)
- **[R3] GIF animation:** `Pillow`'s `.convert("RGB")` collapses animations. → Mitigation: document this in the design; the user can use a short looping MP4 for animations. Documented in `image_display.py` docstring.
- **[R4] `PngDisplay` factory alias lifespan:** keeping the alias forever leaves dead code. → Mitigation: log a deprecation WARNING when an `EffectsSettings.effects` entry contains `"name": "PngDisplay"`; remove in a future major-version change. (Doesn't happen in this PR.)
- **[R5] Pi memory pressure:** a 50 MB MP4 in `VideoDisplay`'s OpenCV `VideoCapture` lands in Pi RAM (the panel is 64×64 — pixel data is small, but the OpenCV decoder keeps intermediate frames). → Mitigation: cap inbound media size at the upload step (reject files > e.g. 8 MB with `HTTP 413` equivalent inside `log_media`, then log + skip). 8 MB is generous for a 10 s 480p MP4.
- **[R6] Race: webhook returns before media is in S3:** The Twilio webhook response is synchronous; the `Message` MQTT publish currently happens inside the request handler after `s3.log_message()`. If we add media, the publish must wait for media too, OR we publish a text-only Message and update it once media lands. → Mitigation: synchronous. `log_media` is small (single-shot HTTP fetch + boto3 put) — sub-second on a typical attachment. The 15 s Twilio response budget is comfortable. (Defer async background upload if the real-world p95 starts to bite.)
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

1. **Twilio webhook response timing — synchronous or async media upload?** The synchronous path is simpler; async (publish text Message immediately, upload media in a background thread, update via a second `type="message-update"` MQTT envelope) is more robust to slow S3 uploads. **Recommendation: synchronous for v1.** Optimize later if p95 webhook latency exceeds the 15 s budget.
2. **Should `MediaCycler` advance the message `hold_seconds` clock at first display, or at "natural media end"?** Current plan: hold clock starts at the `out → in` transition (existing behavior). The cycler shares the hold window with the scroller.
3. **Animated GIF support?** Out of scope for this PR. PIL collapses multi-frame GIFs to single-frame. A follow-up could swap PIL for `imageio` (Pyodide-compatible) and add a frame-tick path.
4. **Audio attachments?** Out of scope. Storage layer (`media/audio/{YYYY-MM}/`) lands here as a no-op; the device would need a future audio path to consume them.
