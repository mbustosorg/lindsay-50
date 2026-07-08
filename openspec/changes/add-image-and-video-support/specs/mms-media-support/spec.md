## ADDED Requirements

### Requirement: Twilio MMS webhook ingests media attachments
The Flask `/api/messages` POST handler MUST detect `NumMedia > 0` in the inbound Twilio webhook form fields and copy each `MediaUrl{i}` to S3 before the TwiML response is constructed.

#### Scenario: Inbound MMS with one image
- **WHEN** Twilio POSTs to `/api/messages` with `NumMedia=1`, `MediaContentType0=image/jpeg`, and a `MediaUrl0=https://api.twilio.com/...`
- **THEN** Flask downloads the bytes via Basic Auth (`AccountSid:AuthToken` from the same `TWILIO_AUTH_TOKEN` env var used for signature validation), writes the bytes to S3 under `media/images/{YYYY-MM}/media-{ISO-timestamp}.{ext}`, and attaches a `media: [{"type": "image/jpeg", "url": "media/images/{YYYY-MM}/media-{ISO-timestamp}.jpg"}]` list to the resulting `Message.to_dict()`.

#### Scenario: Inbound SMS without media
- **WHEN** Twilio POSTs to `/api/messages` with no `NumMedia` field (or `NumMedia=0`)
- **THEN** Flask proceeds exactly as today — no S3 upload, `Message.to_dict()` `media` field is `[]`.

#### Scenario: Inbound MMS with mixed content types
- **WHEN** Twilio POSTs to `/api/messages` with one `image/png` and one `video/mp4` attachment
- **THEN** Flask writes each to its corresponding prefix (`media/images/...` and `media/videos/...`) and emits a `media` list of two entries with the correct `type` strings.

#### Scenario: Twilio MediaUrl returns 410 GONE
- **WHEN** the inbound `MediaUrl*` HTTP fetch returns a non-2xx status (e.g., 410 because Twilio retention expired) OR fails to download within a configurable timeout (default 10s)
- **THEN** Flask logs a WARNING, drops that media item from the list, and the TwiML response proceeds with whatever media landed successfully. A message whose media download fully fails MUST still be persisted (text + empty `media` list).

#### Scenario: S3 upload fails after a successful Twilio fetch
- **WHEN** the bytes downloaded successfully but `boto3.put_object` raises (network, IAM, throttle)
- **THEN** Flask logs a WARNING and the message persists with the affected media item dropped from the `media` list. Other media items in the same webhook still land. The webhook response is still 200 + TwiML.

### Requirement: Message wire carries a media list
The `Message` class in `lib_shared/models.py` MUST round-trip a `media: list[{type, url}]` field through `from_dict` / `to_dict`; existing 4-field messages MUST continue to round-trip unchanged.

#### Scenario: Round-trip with media
- **WHEN** `Message.to_dict()` is called on a message with two media items
- **THEN** the returned dict contains a `media` key with two `{"type": str, "url": str}` entries, each having a non-empty `type` and a `url` matching the S3 key format `media/{images|videos}/{YYYY-MM}/media-{ISO-timestamp}.{ext}`.

#### Scenario: Round-trip without media (backward compat)
- **WHEN** `Message.from_dict({"id": "...", "sender": "...", "body": "...", "received_at": "..."})` is called on a 4-field payload with no `media` key
- **THEN** `message.media == []` and `message.to_dict()["media"] == []`.

#### Scenario: Round-trip with empty media list
- **WHEN** `Message.from_dict({..., "media": []})` is called
- **THEN** `message.media == []`.

#### Scenario: Round-trip preserves type and url
- **WHEN** `Message.from_dict({..., "media": [{"type": "image/jpeg", "url": "media/images/2025-12/foo.jpg"}]})` is called
- **THEN** `message.media == [{"type": "image/jpeg", "url": "media/images/2025-12/foo.jpg"}]` (string equality, no type coercion).

### Requirement: S3 media namespace follows the existing YYYY-MM layout
The S3 key namespace for media MUST use two new prefixes — `media/images/{YYYY-MM}/` and `media/videos/{YYYY-MM}/` — alongside the existing `messages/{YYYY-MM}/` and `config/{YYYY-MM}/` keys.

#### Scenario: Image lands under media/images
- **WHEN** `s3.log_media("image/jpeg", https://api.twilio.com/.../ME19...jpg)` is called and the request succeeds
- **THEN** an S3 object exists at `s3://{bucket}/media/images/{YYYY-MM}/media-{ISO-timestamp}.jpg` with `Content-Type=image/jpeg`.

#### Scenario: Video lands under media/videos
- **WHEN** `s3.log_media("video/mp4", https://api.twilio.com/.../ME20...mp4)` is called and the request succeeds
- **THEN** an S3 object exists at `s3://{bucket}/media/videos/{YYYY-MM}/media-{ISO-timestamp}.mp4` with `Content-Type=video/mp4`.

#### Scenario: Unknown content type
- **WHEN** `s3.log_media` is called with `content_type` not matching `image/*` or `video/*` (e.g., `application/pdf`)
- **THEN** `log_media` logs a WARNING and returns `None` without writing to S3.

#### Scenario: GIF lands under media/images (not media/videos)
- **WHEN** `s3.log_media("image/gif", ...)` is called
- **THEN** the bytes land under `media/images/{YYYY-MM}/...` (gifs are images by MIME type, regardless of multi-frame semantics).

### Requirement: Flask proxies media fetch over authenticated HTTP
The Flask process MUST expose `GET /api/media/<path:s3_key>` for authenticated media fetches; the Pi and browser fetch media via this endpoint rather than direct S3 access.

#### Scenario: Authenticated fetch returns media bytes
- **WHEN** `GET /api/media/media/images/2025-12/media-2025-12-07T15-30-00Z.jpg` is called with the `X-API-Key` header
- **THEN** Flask returns `200 OK` with the bytes from S3 and `Content-Type=image/jpeg` (the same MIME recorded at upload time).

#### Scenario: Unauthenticated fetch denied
- **WHEN** `GET /api/media/media/images/...` is called with no API key
- **THEN** Flask returns `401 Unauthorized`.

#### Scenario: Path traversal blocked
- **WHEN** `GET /api/media/../messages/foo.json` is called (the path contains `..`)
- **THEN** Flask returns `400 Bad Request` and does NOT call S3.

#### Scenario: Missing key
- **WHEN** `GET /api/media/media/images/.../nonexistent.jpg` is called with valid auth
- **THEN** Flask returns `404 Not Found`.

#### Scenario: S3 outage
- **WHEN** boto3 raises during the proxy fetch
- **THEN** Flask returns `502 Bad Gateway` with a JSON body `{ "error": "<message>" }` and logs a WARNING.

### Requirement: Per-message background effect overrides rotation for media messages
The `EffectsCoordinator.tick()` MUST construct a per-message `MediaCycler` Effect whenever the message currently being displayed has a non-empty `media` list; the cycler MUST yield back to the normal rotation after the message's display cycle ends.

#### Scenario: Message with one image
- **WHEN** the coordinator's `get_display_message()` returns the body of a message whose `media` list has one `image/jpeg` entry
- **THEN** on the `out → in` transition the coordinator constructs a `MediaCycler(...)` for that cycle. The cycler renders the image for `max(10s, image_interval_seconds)` (the image's natural interval from `ImageDisplay`'s default of 8 s on a single-image run, with 10 s as the floor for the multi-item cycler), then the cycle ends and the coordinator yields back to `self.effects[self.idx]`.

#### Scenario: Message with multiple media items
- **WHEN** the coordinator's `get_display_message()` returns the body of a message whose `media` list has three entries (two images and one video)
- **THEN** on the `out → in` transition the coordinator constructs a `MediaCycler(media_list)` that picks uniformly at random for `max(10s, item_duration)` per item, switching to the next item via the existing cross-fade path. The cycler cuts off mid-list when `hold_seconds` elapses; remaining items stay saved in S3 but are not shown this cycle.

#### Scenario: Message with no media (existing path)
- **WHEN** `get_display_message()` returns the body of a message with `media == []`
- **THEN** the coordinator's `out → in` transition uses `self.effects[self.idx]` (no MediaCycler). Behavior is identical to today.

#### Scenario: Cycle cutoff respects hold_seconds
- **WHEN** a `MediaCycler` is in active rendering mode and `hold_seconds` (configured to 15 s by default) elapses
- **THEN** the cycler stops advancing through its media list and the coordinator enters the existing `text_out → background` transition. The body of the message fades out; the rotation resumes on the next fresh-message or idle-trigger fade.

#### Scenario: MediaCycler for one short image
- **WHEN** a `MediaCycler` holds a single `image/jpeg` and `hold_seconds=15`
- **THEN** the image renders for the full `hold_seconds` window (15 s) — the cycler's "advance or hold" clock is gated by `hold_seconds`, not by an internal "all items shown" counter.

#### Scenario: MediaCycler item's natural duration read at construction
- **WHEN** a `MediaCycler` is constructed with a `video/mp4` item
- **THEN** it reads `cv2.CAP_PROP_FRAME_COUNT / cv2.CAP_PROP_FPS` once (at construction) to compute the item's natural duration; subsequent cycles reuse the cached value.

### Requirement: MediaCycler Effect integrates with the existing palette and full-frame pipelines
`MediaCycler` MUST subclass `lib_shared.effect_base.Effect` and dispatch internally to `ImageDisplay` (palette) for image/* items and `VideoDisplay` (full-frame) for video/* items.

#### Scenario: Image item renders through palette pipeline
- **WHEN** the cycler's current item is `image/jpeg`
- **THEN** the cycler delegates `tick` and `render` to an internal `ImageDisplay` instance whose `set_brightness` calls propagate to the cycler's palette. The palette pixels appear on the panel.

#### Scenario: Video item renders through full-frame pipeline
- **WHEN** the cycler's current item is `video/mp4`
- **THEN** the cycler delegates `tick` and `render` to an internal `VideoDisplay` instance; the cycler's `set_brightness` is stored as a factor and applied when blitting via `canvas.SetImage(image.point(...))`.

#### Scenario: Cross-item transition honors the current brightness
- **WHEN** the cycler swaps from item A to item B mid-fade (during a coordinator-driven out → in)
- **THEN** both internal renderers receive the same `set_brightness(b)` call from the coordinator (the cycler forwards, doesn't multiply). The swap is instantaneous from the coordinator's perspective — the cycler picks a new item and renders it on the next `tick()`.
