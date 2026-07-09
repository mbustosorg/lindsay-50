## 1. Message model + wire shape

- [ ] 1.1 Add `media` field to `Message` in `lib_shared/models.py` (default `[]`, round-trip via `from_dict`/`to_dict`, `media` is a list of `{"type": str, "url": str}` dicts).
- [ ] 1.2 Update `MessageView.to_dict()` to include the media list verbatim.
- [ ] 1.3 Write `tests/message_wire_media_test.py` covering: round-trip with media, round-trip without media (backward compat), round-trip with `[]`, type/url preservation.

## 2. S3 media storage

- [ ] 2.1 Add `_MEDIA_KEY_TEMPLATE_IMAGES` and `_MEDIA_KEY_TEMPLATE_VIDEOS` constants in `heart-message-manager/s3.py` (mirror existing `_MESSAGE_KEY_TEMPLATE` shape: `media/{images|videos}/{year}-{month}/media-{datetime}.{ext}`).
- [ ] 2.2 Add `MEDIA_KEY_PREFIXES = ("media/images/", "media/videos/")` constant for the rebuild-from-S3 path's skip filter.
- [ ] 2.3 Add `s3.log_media(content_type: str, source_url: str) -> str | None`: HTTP-Basic-auth download via `TWILIO_AUTH_TOKEN`, write to S3 under the correct prefix, return the S3 key on success.
- [ ] 2.4 Add `s3.signed_media_url(s3_key: str) -> str | None`: returns `boto3.client("s3").generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)` (or `None` on failure). Used by Flask to mint a fresh signed URL on each fetch.
- [ ] 2.5 Wire `MEDIA_KEY_PREFIXES` skip filter into `heart-message-manager/sqlite.py` (or wherever the S3 rebuild lives) so a `rebuild_from_s3` start doesn't mistake `media/...` keys for message-body files.

## 3. Flask webhook ingestion (async media capture)

**Sync phase (request handler — respond to Twilio fast):**

- [ ] 3.1 In `heart-message-manager/main.py:_process_inbound_message`, detect `NumMedia > 0` from `request.form`.
- [ ] 3.2 Register the inbound `MessageSid` in the dedupe guard (in-process `dict[MessageSid, threading.Event]` keyed by SID; if the SID is already in flight, return 200/TwiML immediately without spawning a new worker).
- [ ] 3.3 If `NumMedia > 0`, spawn a daemon `threading.Thread` (target: `_process_inbound_media_async`) carrying the form fields + a reference to the dedupe guard entry. Don't `join()`.
- [ ] 3.4 Return 200/TwiML to Twilio immediately (no media download in the request path).
- [ ] 3.5 Change the empty-body gate to `if not body and not media_attached: return 204` (or equivalent): empty body with non-empty `media` is accepted (async thread will publish the message); empty body with no media still 204s.

**Async phase (`_process_inbound_media_async` background thread):**

- [ ] 3.6 Use a `concurrent.futures.ThreadPoolExecutor(max_workers=min(len(media), 4))` to download + upload each `MediaUrl{i}` in parallel. Each worker calls `s3.log_media(content_type, url)` (already does Basic Auth + boto3 put) and returns `(i, s3_key_or_None)` or raises.
- [ ] 3.7 Collect results; build the `media: list[{type, url}]` list, dropping items where download/upload failed (logged WARNING).
- [ ] 3.8 Persist the `Message` (text + completed media list) to S3 + SQLite (existing `s3.log_message` + sqlite path).
- [ ] 3.9 Publish the `MessageEnvelope` over MQTT exactly once.
- [ ] 3.10 Release the dedupe-guard entry; signal completion. Wrap the whole thread body in try/except so an unhandled exception logs CRITICAL but doesn't leave the dedupe guard pinned.

**Media-fetch endpoint:**

- [ ] 3.11 Add the new `GET /api/media/<path:s3_key>` route in `heart-message-manager/main.py` (auth via `api_login_required`, calls `s3.signed_media_url(s3_key)`, returns `302 Found` with `Location: <signed-s3-url>` header — no bytes through Flask).
- [ ] 3.12 Add path-traversal guard (`..` rejected with 400) on the media-fetch route.

## 4. Tests for MMS ingestion + media proxy

- [ ] 4.1 Write `tests/mms_media_test.py`: POST `/api/messages` with a mocked Twilio payload (NumMedia=1, image/jpeg); assert the response is 200/TwiML returned *before* the S3 upload completes (no `join()`); wait on the dedupe-guard entry; then assert S3 was hit and the Message has a 1-item media list with the expected S3 key shape.
- [ ] 4.2 Add S3 failure-path test: `log_media` returning `None` for one of N attachments → webhook still 200s; after dedupe release, Message has an `N-1` media list (failed item dropped, WARNING logged).
- [ ] 4.3 Add `tests/media_proxy_test.py`: 302 happy path (with stubbed `s3.signed_media_url` returning a fake signed URL, assert the response is 302 + `Location:` header pointing to it), 401 unauthenticated, 400 path-traversal, 404 when `signed_media_url` raises, 502 S3 outage.
- [ ] 4.4 Add `tests/mms_async_test.py`: webhook response latency (assert < 200 ms with stubbed S3 / Twilio); `MessageSid` dedupe (POST same SID twice → second call returns 200 immediately, only one background thread runs); background-thread crash leaves dedupe-guard released (next webhook with same SID doesn't deadlock).
- [ ] 4.5 Add `tests/media_cycler_codec_test.py`: 1-item media where the inner renderer raises `cv2.error` on first frame → cycler drops the item, the list is empty, coordinator's `self.effects[self.idx]` takes over. 3-item media where item 1 is bad codec → cycler drops item 1, advances to item 2 normally.

## 5. ImageDisplay class rename + format expansion (internal-only — removed from effects registry)

- [ ] 5.1 Create `lib_shared/patterns/image_display.py` with the `ImageDisplay` class (rename from `PngDisplay`). Same palette rendering through indexed `Bitmap`/`Palette`. Glob supports `*.png, *.jpg, *.jpeg, *.gif, *.webp`. **The class is an inner renderer consumed by `MediaCycler` via direct import — it is NOT an entry in the effects registry.**
- [ ] 5.2 Loader switch: for PNG keep the alpha-mask-on-white path; for JPEG/GIF/WebP use `convert("RGB")` (drop alpha). Detect alpha via `img.mode == "RGBA"` and only apply the mask when present.
- [ ] 5.3 Update `lib_shared/config/effects_settings.json` (the canonical JSON landed by PR-53): **remove** the existing `PngDisplay` entry AND any listed-but-disabled `VideoDisplay` entry from the `effects` list. The canonical now carries only the 5 non-media effects (Hyperspace, Honeycomb, Flame, Fireworks, NightSky).
- [ ] 5.4 Delete `lib_shared/patterns/png_display.py` AND its browser-preview symlink at `heart-message-manager/static/preview/lib_shared/patterns/png_display.py` (post-PR-53 the preview tree symlinks patterns directly; verify with `ls -la`).
- [ ] 5.5 Update test fixtures: `tests/patterns_import_test.py` drops the `ImageDisplay` and `VideoDisplay` import assertions (they're no longer in the registry); the test now covers the 5 non-media effects. `tests/effects_settings_test.py`, `tests/api_config_validation_test.py`, `tests/preview_wiring_test.py` no longer reference `ImageDisplay`/`VideoDisplay` in the canonical-JSON fixtures. Add a test asserting `make_effect_class("PngDisplay")` and `make_effect_class("ImageDisplay")` both return `None` and emit the existing unknown-name WARNING (verifies legacy operator-override entries land gracefully).
- [ ] 5.6 Write `tests/image_display_test.py`: format discovery (PNG + JPEG + GIF + WebP under one directory), single-image hold forever, multi-image crossfade, corrupt file → blank panel + WARNING.

## 6. MediaCycler per-message background effect

- [ ] 6.1 Create `lib_shared/patterns/media_cycler.py` with the `MediaCycler` Effect subclass. Constructor accepts `message_id: str` + the message's `media: list[{type, url}]`.
- [ ] 6.2 Resolve each `media[*].url` (S3 key) to a local file path via the Flask proxy (`cfg.API_BASE_URL + "/api/media/" + key`). Cache the path across cycles for the same key.
- [ ] 6.3 Construct an `ImageDisplay` for image/* mime types (per-item, with the S3-fetched path as the only file in the directory or as a single-path override); construct a `VideoDisplay` for video/* mime types.
- [ ] 6.4 Per-item natural-duration read for video (`cv2.CAP_PROP_FRAME_COUNT / cv2.CAP_PROP_FPS`) at construction; cache the duration alongside the path.
- [ ] 6.5 Cycle advance: pick uniformly at random from `not-yet-shown-this-cycle` items; mark items as "shown this cycle". Cut off at `hold_seconds` (from `EffectsSettings`).
- [ ] 6.6 `set_brightness(b)` forwards to the active internal renderer.
- [ ] 6.7 `tick()` advances the active internal renderer.
- [ ] 6.8 `render(canvas)` forwards to the active internal renderer.
- [ ] 6.9 **Codec-failure handling (Design D12):** wrap the inner renderer's `tick()` / `render()` calls in try/except. On `cv2.error`, `PIL.UnidentifiedImageError`, `OSError`, or any decode-related exception, log WARNING (`"MediaCycler: dropping item %r due to decode failure: %s"`), remove the item from `self._items`, and pick a new one on the next advance. If `self._items` becomes empty, signal the coordinator to fall back to `self.effects[self.idx]` (rotation) on the next fade (same path as the existing `hold_seconds` cutoff).
- [ ] 6.10 Frame-by-frame video reads: `VideoDisplay`'s frame loop uses `VideoCapture.grab()` + `retrieve()` (not `read()`), so per-frame memory is bounded by frame dimensions, not total video size. No upload-time size cap.

## 7. EffectsCoordinator media override

- [ ] 7.1 Extend `lib_shared/message_manager.py` to expose message media (e.g., `get_message_media(id) -> list[dict]` or extend the existing `get_messages(limit=...)` to return `Message` not just `body`).
- [ ] 7.2 In `lib_shared/effects_coordinator.py:EffectsCoordinator`, at the `out → in` transition (in `tick()`), fetch the media list for the message currently being displayed (the one whose body just hit the scroller).
- [ ] 7.3 If media is non-empty, construct a `MediaCycler(media_list)` and assign to `self.current` in place of `self.effects[self.idx]`. The coordinator's existing `set_brightness` calls continue to drive the cycler.
- [ ] 7.4 When the cycler's cycle ends OR `hold_seconds` elapses, fall back to `self.effects[self.idx]` on the next fade (existing `text_out → background → rotation` path).
- [ ] 7.5 When the message has no media, behavior is unchanged — coordinator uses `self.effects[self.idx]`.
- [ ] 7.6 Empty-text branch: when the displayed message has `body == ""` AND non-empty `media`, the existing `scroller.set_text("", display.width)` path in the `out → in` transition handles the blank-text case (`showing_text=False`, mode is `background` after fade-in instead of `hold`). Verify the coordinator does NOT enter the `hold` mode with an empty text.

## 8. Tests for the EffectsCoordinator media override

- [ ] 8.1 Write `tests/media_cycler_test.py`: 1-item media, 3-item media (cut off by `hold_seconds`), no media (rotation path), GIF vs JPEG vs MP4 dispatch.
- [ ] 8.2 Update `tests/effects_coordinator_test.py` (if present) to cover the "current message has media" branch in the `out → in` transition.

## 9. Browser preview support

- [ ] 9.1 Verify the existing `heart-message-manager/preview_main.py` `MediaCycler`-equivalent path: the PyScript side reads `Message.to_dict()['media']` via `js.JSON.parse` (already in the wire), constructs `<img src="/api/media/<key>">` and `<video src="/api/media/<key>">` for the current cycle item.
- [ ] 9.2 Test the preview against a stubbed `/api/media/<key>` to confirm the cross-fade timing holds.

## 10. Admin UI thumbnails

- [ ] 10.1 In `heart-message-manager/templates/messages.html`, render a thumbnail strip below each row that has media. Each thumbnail `<img>` src is `cfg.API_BASE_URL + "/api/media/" + key`.
- [ ] 10.2 Add a click-to-zoom modal (full-size image) using the existing modal CSS.

## 11. Documentation + closeout

- [ ] 11.1 Update `CLAUDE.md` if needed (note: image formats supported, MMS handling, Flask 302 signed-URL endpoint, JSON-driven effects registry from PR-53).
- [ ] 11.2 Update the issue #38 body to include `openspec_change_name: add-image-and-video-support`.
- [ ] 11.3 Add a brief note to `heart-message-manager/README.md` (if it exists) about the new `/api/media/...` 302 endpoint, the S3 `media/` prefix, and `ImageDisplay` in `lib_shared/config/effects_settings.json`.
- [ ] 11.4 Run `PYTHONPATH=. pytest tests/ -v` end-to-end and confirm no regressions. Verify the post-PR-53 `tests/effects_loader_test.py` and `tests/test_admin_settings_route.py` (PR-53 additions) still pass.
- [ ] 11.5 _(Future enhancement — out of scope for this PR.)_ Pre-cache media at message-receive time: after `s3.log_media(...)` succeeds, stream the bytes to `/var/cache/lindsay-50/<sha256(key)>.<ext>` on the Pi and have `MediaCycler` look there first before falling back to the Flask redirect. Wire shape is unchanged.
