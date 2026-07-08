## 1. Message model + wire shape

- [ ] 1.1 Add `media` field to `Message` in `lib_shared/models.py` (default `[]`, round-trip via `from_dict`/`to_dict`, `media` is a list of `{"type": str, "url": str}` dicts).
- [ ] 1.2 Update `MessageView.to_dict()` to include the media list verbatim.
- [ ] 1.3 Write `tests/message_wire_media_test.py` covering: round-trip with media, round-trip without media (backward compat), round-trip with `[]`, type/url preservation.

## 2. S3 media storage

- [ ] 2.1 Add `_MEDIA_KEY_TEMPLATE_IMAGES` and `_MEDIA_KEY_TEMPLATE_VIDEOS` constants in `heart-message-manager/s3.py` (mirror existing `_MESSAGE_KEY_TEMPLATE` shape: `media/{images|videos}/{year}-{month}/media-{datetime}.{ext}`).
- [ ] 2.2 Add `MEDIA_KEY_PREFIXES = ("media/images/", "media/videos/")` constant for the rebuild-from-S3 path's skip filter.
- [ ] 2.3 Add `s3.log_media(content_type: str, source_url: str) -> str | None`: HTTP-Basic-auth download via `TWILIO_AUTH_TOKEN`, write to S3 under the correct prefix, return the S3 key on success.
- [ ] 2.4 Add `s3.proxy_media(s3_key: str) -> bytes | None`: boto3 `get_object` returning the bytes (or `None` on failure).
- [ ] 2.5 Wire `MEDIA_KEY_PREFIXES` skip filter into `heart-message-manager/sqlite.py` (or wherever the S3 rebuild lives) so a `rebuild_from_s3` start doesn't mistake `media/...` keys for message-body files.

## 3. Flask webhook ingestion (media capture)

- [ ] 3.1 In `heart-message-manager/main.py:_process_inbound_message`, detect `NumMedia > 0` from `request.form`.
- [ ] 3.2 For each `MediaUrl{i}` / `MediaContentType{i}` pair, call `s3.log_media(content_type, url)`. Wrap in try/except so a media failure does NOT abort the request.
- [ ] 3.3 Pass the resulting `media: list[{type, url}]` list to the `Message(...)` constructor.
- [ ] 3.4 Add the new `GET /api/media/<path:s3_key>` route in `heart-message-manager/main.py` (auth via `api_login_required`, calls `s3.proxy_media(s3_key)`, returns bytes with the original `Content-Type`).
- [ ] 3.5 Add path-traversal guard (`..` rejected with 400) on the proxy route.

## 4. Tests for MMS ingestion + media proxy

- [ ] 4.1 Write `tests/mms_media_test.py`: POST `/api/messages` with a mocked Twilio payload (NumMedia=1, image/jpeg), assert S3 was hit, Message has a 1-item media list with the expected S3 key shape.
- [ ] 4.2 Add S3 failure-path test: `log_media` returning `None` → webhook still 200s; Message has empty media list.
- [ ] 4.3 Add `tests/media_proxy_test.py`: 200 happy path with a stubbed `s3.proxy_media`, 401 unauthenticated, 400 path-traversal, 404 missing key, 502 S3 outage.

## 5. ImageDisplay rename + format expansion

- [ ] 5.1 Create `lib_shared/patterns/image_display.py` with the `ImageDisplay` class (rename from `PngDisplay`). Same palette rendering through indexed `Bitmap`/`Palette`. Glob supports `*.png, *.jpg, *.jpeg, *.gif, *.webp`.
- [ ] 5.2 Loader switch: for PNG keep the alpha-mask-on-white path; for JPEG/GIF/WebP use `convert("RGB")` (drop alpha). Detect alpha via `img.mode == "RGBA"` and only apply the mask when present.
- [ ] 5.3 Update `lib_shared/effects_factory.py:make_effect_class`: `make_effect_class("PngDisplay")` returns `ImageDisplay` (with a deprecation WARNING log); add `make_effect_class("ImageDisplay") -> ImageDisplay`.
- [ ] 5.4 Update `lib_shared/models.py:_DEFAULT_EFFECTS_LIST_FULL`: replace `{"name": "PngDisplay", "enabled": False}` with `{"name": "ImageDisplay", "enabled": True}`.
- [ ] 5.5 Delete `lib_shared/patterns/png_display.py`.
- [ ] 5.6 Update test fixtures: `tests/patterns_import_test.py`, `tests/effects_settings_test.py`, `tests/api_config_validation_test.py`, `tests/preview_wiring_test.py` — flip from `"PngDisplay"` to `"ImageDisplay"` and add an assertion that the factory alias still resolves `"PngDisplay"` to `ImageDisplay`.
- [ ] 5.7 Write `tests/image_display_test.py`: format discovery (PNG + JPEG + GIF + WebP under one directory), single-image hold forever, multi-image crossfade, corrupt file → blank panel + WARNING.

## 6. MediaCycler per-message background effect

- [ ] 6.1 Create `lib_shared/patterns/media_cycler.py` with the `MediaCycler` Effect subclass. Constructor accepts `message_id: str` + the message's `media: list[{type, url}]`.
- [ ] 6.2 Resolve each `media[*].url` (S3 key) to a local file path via the Flask proxy (`cfg.API_BASE_URL + "/api/media/" + key`). Cache the path across cycles for the same key.
- [ ] 6.3 Construct an `ImageDisplay` for image/* mime types (per-item, with the S3-fetched path as the only file in the directory or as a single-path override); construct a `VideoDisplay` for video/* mime types.
- [ ] 6.4 Per-item natural-duration read for video (`cv2.CAP_PROP_FRAME_COUNT / cv2.CAP_PROP_FPS`) at construction; cache the duration alongside the path.
- [ ] 6.5 Cycle advance: pick uniformly at random from `not-yet-shown-this-cycle` items; mark items as "shown this cycle". Cut off at `hold_seconds` (from `EffectsSettings`).
- [ ] 6.6 `set_brightness(b)` forwards to the active internal renderer.
- [ ] 6.7 `tick()` advances the active internal renderer.
- [ ] 6.8 `render(canvas)` forwards to the active internal renderer.

## 7. EffectsCoordinator media override

- [ ] 7.1 Extend `lib_shared/message_manager.py` to expose message media (e.g., `get_message_media(id) -> list[dict]` or extend the existing `get_messages(limit=...)` to return `Message` not just `body`).
- [ ] 7.2 In `lib_shared/effects_coordinator.py:EffectsCoordinator`, at the `out → in` transition (in `tick()`), fetch the media list for the message currently being displayed (the one whose body just hit the scroller).
- [ ] 7.3 If media is non-empty, construct a `MediaCycler(media_list)` and assign to `self.current` in place of `self.effects[self.idx]`. The coordinator's existing `set_brightness` calls continue to drive the cycler.
- [ ] 7.4 When the cycler's cycle ends OR `hold_seconds` elapses, fall back to `self.effects[self.idx]` on the next fade (existing `text_out → background → rotation` path).
- [ ] 7.5 When the message has no media, behavior is unchanged — coordinator uses `self.effects[self.idx]`.

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

- [ ] 11.1 Update `CLAUDE.md` if needed (note: image formats supported, MMS handling, Flack proxy endpoint).
- [ ] 11.2 Update the issue #38 body to include `openspec_change_name: add-image-and-video-support`.
- [ ] 11.3 Add a brief note to `heart-message-manager/README.md` (if it exists) about the new `/api/media/...` endpoint and the S3 `media/` prefix.
- [ ] 11.4 Run `PYTHONPATH=. pytest tests/ -v` end-to-end and confirm no regressions.
