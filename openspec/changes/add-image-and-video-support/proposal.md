## Why

Today `heart-message-manager/main.py:api_messages()` strips the inbound Twilio webhook down to `From` + `Body` and discards everything in `NumMedia*` / `MediaUrl*` / `MediaContentType*`. The two effect classes that *can* render attached media — `PngDisplay` and `VideoDisplay` in `lib_shared/patterns/` — are disabled by default in `lib_shared/models.py:_DEFAULT_EFFECTS_LIST_FULL` because they only know how to consume files from `<repo>/design/{pngs,videos}/`, which is operator-supplied, not message-driven. The result: when a user sends an SMS/MMS with a photo, the picture silently disappears, the message text scrolls on a Flame/NightSky background, and the operator has to physically copy the file onto the Pi and reboot for it to ever appear.

This change wires MMS media end-to-end: capture Twilio's `MediaUrl0..N` + `MediaContentType0..N`, immediately copy each attachment to S3 (Twilio's retention is short and reading past it returns 410), carry the (type, URL) list on the existing `Message` wire, and teach the `EffectsCoordinator` to use that media as the background for the message's display cycle — falling back to the rotation when there's no media.

While doing the image work we'll rename `PngDisplay` → `ImageDisplay` and add JPEG/GIF/WebP support via PIL — the expand is local (PIL handles all four), the rename drops the misleading "Png" suffix the moment the class grows beyond PNG, and the cost is mostly import paths and one `_DEFAULT_EFFECTS_LIST_FULL` entry.

## What Changes

**Twilio ingestion (`heart-message-manager/main.py:_process_inbound_message`)**

- Detect `NumMedia` > 0 from `request.form`. For each `MediaContentType{i}` / `MediaUrl{i}` pair, **download the bytes immediately and write them to OUR S3 under `media/images/{YYYY-MM}/...` (for `image/*`) or `media/videos/{YYYY-MM}/...` (for `video/*`)** — authenticated via Twilio Basic Auth (`AccountSid:AuthToken` from the same `TWILIO_AUTH_TOKEN` env var used for webhook signature validation). The bytes are pulled and persisted before the TwiML response is constructed; Twilio's retention policy is irrelevant to us once the bytes are in our bucket. GIF is `image/gif` and lands under `media/images/`.
- The wire tuple stored on `Message.media[*].url` is the **S3 key in our bucket** (e.g. `media/images/2025-12/media-2025-12-07T15-30-00Z.jpg`) — **not** the original Twilio `MediaUrl*`. After the bytes are copied, the Twilio URL is discarded; the wire never references Twilio's storage again. The Pi and browser fetch from OUR S3 via the Flask proxy.
- Match the existing message-logging fire-and-forget tone: try/except around the S3 write, log a WARNING and continue with an empty media list if S3 fails. The text always ships even if the media upload breaks — the operator sees a graceful text-only background, not a missing message.
- **Empty body with media is accepted; empty body + no media still 204.** Today `_process_inbound_message` returns 204 when `Body` is empty, which would drop a media-only MMS. Change the gate: accept the message iff `body` is non-empty OR `NumMedia > 0`; persist (text + media list) via S3 and MQTT. A media-only message flows through the coordinator with `body=""` and the existing `scroller.set_text("", display.width)` path in the `out → in` transition handles the blank-text case — the `MediaCycler` is the background, the scroller renders nothing, hold and rotation proceed normally.

**Message model (`lib_shared/models.py:Message`)**

- Add `media: list[dict]` (default `[]`) to `Message`. Each entry is `{"type": <mime>, "url": <s3-key>}`. Existing 4-field messages round-trip unchanged: `from_dict` defaults the field to `[]` when absent; `to_dict` always emits it.
- The wire shape grows by exactly one optional top-level array. Per-message filtering (`MessageView`) carries the media list verbatim — no filter behavior changes; suppressed messages just disappear, media and all.

**S3 storage (`heart-message-manager/s3.py`)**

- New `_MEDIA_KEY_TEMPLATE_IMAGES = "media/images/{year}-{month}/{basename}-{datetime}{ext}"` and `_MEDIA_KEY_TEMPLATE_VIDEOS = "media/videos/{year}-{month}/{basename}-{datetime}{ext}"`. Per the issue: a `/videos` and `/images` directory under the bucket, with the same `YYYY-MM` folder strategy. The `media/` prefix under each keeps `media/images/` and `media/videos/` distinct from any future `media/audio/`.
- Two new helpers: `log_media(content_type: str, source_url: str) -> str | None` (download → write to S3 → returns the S3 key on success or `None` on failure) and `proxy_media(s3_key: str) -> bytes | None` (for the Flask proxy endpoint below). `log_messages` S3 key templates and `load_messages_from_s3` stay exactly as-is — the existing message-body files are a different S3 prefix from media.
- New `MEDIA_KEY_PREFIXES = ("media/images/", "media/videos/")` for the existing `load_messages_from_s3`-style rebuild path, so a `rebuild_from_s3` start that scans all `messages/` and `media/` prefixes doesn't trip over media files (it does NOT scan media files; only the message-json files).

**Image fetch path (Flask proxy)**

- The Pi (Raspberry Pi) does not have AWS credentials in `settings.toml`; it would need a network round-trip to fetch media even if it did. The browser preview runs entirely client-side and has no S3 SDK. The existing Flask process has boto3 + AWS creds already, so it serves media via a new authenticated proxy:
  - `GET /api/media/<path:s3_key>` — `api_login_required`. Internally calls `s3.proxy_media(s3_key)`, streams the bytes back with the original `Content-Type` from the lookup.
  - The S3 key stored in the `Message.media[*].url` field is the *logical* key (`media/images/2025-12/img-…jpg`). Callers (Pi, browser) construct the proxy URL via `cfg.API_BASE_URL + "/api/media/" + s3_key`.
- Pre-signed S3 URLs were considered and rejected: they expire (default 1h), they're a footgun for any caller that caches the URL, and they require the Pi to know the signing region. The Flask proxy trades a small HTTP hop for never-expiring keys and one auth surface.

**Image / Video display (`lib_shared/patterns/png_display.py`, `lib_shared/patterns/video_display.py`, `lib_shared/effects_factory.py`)**

- Rename `PngDisplay` → `ImageDisplay` (file + class). The class already supports palette-based rendering through the indexed Bitmap path, so a JPEG/GIF/WebP just changes the load: replace `Image.open(path).convert("RGBA")` with `Image.open(path).convert("RGB")` (drop the alpha), and replace the `glob("*.png")` with `glob("*")` filtered to a `_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")` tuple. GIF is a special case: PIL's `.convert("RGB")` discards the animation; keep it single-frame unless a follow-up change asks for animated GIF. File rename to `image_display.py`.
- Update `lib_shared/effects_factory.py:make_effect_class`: the `"PngDisplay"` key returns the new `ImageDisplay` class; add `"ImageDisplay"` as an alias for the same class. Keep `"PngDisplay"` for one cycle so old `EffectsSettings.effects` entries don't break.
- Update `lib_shared/models.py:_DEFAULT_EFFECTS_LIST_FULL`: change `{"name": "PngDisplay", "enabled": False}` to `{"name": "ImageDisplay", "enabled": True}` so the rename ships by default (the operator only has to flip the existing PNG enabled-flag off). Defaults: **ImageDisplay ON, VideoDisplay OFF** because image attachments are far more common than video on MMS, and `design/pngs/` already has curated content.
- `VideoDisplay` stays as-is (already supports MP4/MOV/AVI/MKV/WebM/GIF). The constructor's `path` argument keeps working; per-message we instantiate it with the downloaded media path.

**Per-message background override (`lib_shared/effects_coordinator.py`)**

- The coordinator already has a `get_display_message()` that returns the body to scroll. Add a sibling `get_display_media(message_id) -> list[dict] | None` (or extend the message pull to also return the media) that returns the media list of the *currently-displayed* message (the one whose body is on the scroller). When non-empty, the coordinator's `tick()` constructs an ad-hoc Effect for *this one display cycle*: a `MediaCycler` that holds a list of media items, picks one at a time, instantiates either an `ImageDisplay` (image/* mime) or a `VideoDisplay` (video/* mime) for the picked item, and renders through it.
  - `MediaCycler.media_hold_seconds = max(10.0, media_duration_or_image_interval)`. The image case uses the existing `ImageDisplay` defaults (currently 8 s); the video case uses the source video duration (read once via OpenCV's `cv2.CAP_PROP_FRAME_COUNT` / `cv2.CAP_PROP_FPS` at construction).
  - Multiple media items: cycle randomly each `media_hold_seconds`. **Decision: cut off at `hold_seconds`** rather than extend it. Issue flagged "either extend hold_seconds, or just cut off the display" — extending makes the timing unpredictable (a 10-minute hold vs the configured 15s hold); cutting off matches the existing rotation timeout behavior. The user sees each media item for at least its natural duration (or 10 s, whichever is longer); if the message had 8 photos and only 3 fit in `hold_seconds=15s`, the user sees 3. The remaining 5 are persisted in S3 (per the storage contract) and the operator can view the full set via the admin UI.
  - When the message has fewer than 2 media items, the cycler just renders the one item for the natural duration (or 10 s for images) — `hold_seconds` still applies as a hard ceiling.
  - When the message has no media, the coordinator falls back to `self.effects[self.idx]` (the current rotation behavior). No behavioral change for SMS-only messages.

**Wire format (admin UI + browser)**

- The `/messages` admin view shows a small thumbnail strip below each message with media (admin fetches the proxy URL with the existing API key auth). Click a thumbnail for full-size in a modal.
- The browser preview's `MessageView` already round-trips `to_dict()` → `js.JSON.parse`; the media list is read by the preview's `MediaCycler` (a browser-suitable stand-in for the Pi-side `MediaCycler`, sharing the same constructor signature). The browser preview becomes a useful place to QA media rendering without touching the Pi.

## Capabilities

### New Capabilities

- `mms-media-support`: Twilio MMS webhook ingestion captures `NumMedia` + `MediaContentType{i}` + `MediaUrl{i}`, downloads each attachment immediately, copies to S3 under `media/images/{YYYY-MM}/` or `media/videos/{YYYY-MM}/` (per content type) before the Twilio retention window expires, and attaches a `(type, s3_key)` list to the inbound `Message`. The `Message` wire grows an optional `media: list[{type, url}]` array (defaulting to `[]` for backward compat). The Pi and browser fetch media through a new authenticated Flask proxy `GET /api/media/<path:s3_key>`. The `EffectsCoordinator` routes messages with media to a per-message `MediaCycler` (constructs `ImageDisplay` or `VideoDisplay` per item based on mime, cycles every `max(10 s, media_duration)` up to `hold_seconds`, then yields to the rotation). MMS-only path — pure SMS messages get no new surface.
- `image-display-pattern`: Rename `lib_shared/patterns/png_display.py` → `image_display.py` and the class `PngDisplay` → `ImageDisplay`. Same palette-based rendering through the indexed `Bitmap`/`Palette` pipeline; the load path drops the alpha-channel trick (JPEG has no alpha; GIF's anim is collapsed to single frame, same as PNG). Supports PNG/JPEG/GIF/WebP via PIL — same module deps it already has. The `EffectsSettings.effects` list default updates from `"PngDisplay": false` to `"ImageDisplay": true` so the rename ships on by default; the curl-friendly alias `"PngDisplay"` is kept in `effects_factory.make_effect_class` for one release cycle so old configs don't break. `VideoDisplay` is untouched in this capability.

### Modified Capabilities

_None. There are no prior capability requirements on this repo (the `openspec/specs/` tree is empty); every behavior change above is a new surface._

## Impact

- **New files:**
  - `lib_shared/patterns/image_display.py` — the renamed + format-expanded slideshow class.
  - `lib_shared/patterns/media_cycler.py` — the per-message ad-hoc background effect. Reads the message's media list; constructs `ImageDisplay` or `VideoDisplay` per item; cycles each item for `max(10 s, media_duration)`; cuts off at `hold_seconds`.
  - `tests/mms_media_test.py` — webhook → S3 round-trip (with a stubbed Twilio POST), message wire `from_dict`/`to_dict` with and without media, the `MediaCycler`'s advance per hold window, and the image-format glob.
  - `tests/image_display_test.py` — rename verification, JPEG/GIF/WebP load paths, multi-format directory globs.
  - `tests/media_proxy_test.py` — `GET /api/media/<key>` happy path + 404 + 401.
- **Modified files:**
  - `heart-message-manager/main.py` — `_process_inbound_message` detects `NumMedia > 0`, calls `s3.log_media(...)` per attachment, builds `Message(..., media=[...])`, attaches the media list to `Message.to_dict()`. New `GET /api/media/<path:s3_key>` route handler.
  - `heart-message-manager/s3.py` — `_MEDIA_KEY_TEMPLATE_IMAGES`/`_MEDIA_KEY_TEMPLATE_VIDEOS`, `log_media(...)`, `proxy_media(...)`, `MEDIA_KEY_PREFIXES` constant.
  - `lib_shared/models.py` — `Message` adds `media` field; `_DEFAULT_EFFECTS_LIST_FULL` flips `PngDisplay → ImageDisplay, enabled=True`. No change to `MessageEnvelope` (the envelope still wraps `Message.to_dict()`).
  - `lib_shared/effects_factory.py` — `"PngDisplay"` returns the new `ImageDisplay` class; add `"ImageDisplay"`; no other key changes.
  - `lib_shared/effects_coordinator.py` — add `_last_shown_message_id`-keyed media lookup; construct `MediaCycler` for non-empty media lists in the `out→in` transition; yield back to the rotation when the cycler's window closes.
  - `lib_shared/message_manager.py` — `messages.get_message_by_id(id)` (or extend `get_messages`) so the coordinator can fetch a specific message's media; existing `get_messages(limit=...)` only returns bodies.
  - `heart-message-manager/templates/messages.html` — render the media strip (thumbnails) per row.
  - `heart-message-manager/preview_main.py` — the browser preview's `MediaCycler` is a thin PyScript-compatible wrapper that calls `Message.to_dict()`'s media list and constructs JS-side proxies to `/api/media/<key>`.
  - `tests/patterns_import_test.py`, `tests/effects_settings_test.py`, `tests/api_config_validation_test.py`, `tests/preview_wiring_test.py` — fixtures flip from `"PngDisplay"` to `"ImageDisplay"` (or assert both names coexist).
  - `lib_shared/patterns/png_display.py` — DELETE (renamed to `image_display.py`).
- **Settings / config:**
  - `heart-matrix-controller/settings.toml` — no new keys. The Pi constructs `ImageDisplay`/`VideoDisplay`/`MediaCycler` from the wire (per-message media URLs); the existing `PNG_DIR` / `VIDEO_PATH` env-overrides continue to be the default-content fallback for the rotation's stand-alone mode (when no message has media).
  - The `flask-management-app` and `add-sign-preview-rendering` changes are not impacted: the message wire addition is additive, the effects rotation is unchanged for SMS-only messages, and the new `MediaCycler` follows the existing `Effect` interface.
- **Dependencies:** No new libraries. PIL handles the new image formats already (Pillow is already a dependency for `PngDisplay`). OpenCV is already a dependency for `VideoDisplay`. boto3 is already in `requirements-flask.txt`. Twilio auth for `MediaUrl*` downloads uses the existing `TWILIO_AUTH_TOKEN` env var (Basic Auth — Twilio's `MediaUrl` endpoints accept HTTP Basic with `AccountSID:AuthToken`; we already have the token in config).
- **S3 key namespace collision risk:** `media/images/` and `media/videos/` are new prefixes; no existing keys live there. The existing `messages/{year}-{month}/` and `config/{year}-{month}/` keys are untouched.
