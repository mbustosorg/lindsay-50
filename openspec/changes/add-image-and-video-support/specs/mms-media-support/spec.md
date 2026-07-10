## ADDED Requirements

### Requirement: Twilio MMS webhook ingests media attachments asynchronously
The Flask `/api/messages` POST handler MUST respond to Twilio with 200/TwiML immediately on MMS payloads (no blocking on media downloads), then process media in a background thread. The background thread MUST download each `MediaUrl{i}` via Twilio Basic Auth, upload to S3, persist the resulting `Message` (text + completed `media` list), and publish the `MessageEnvelope` over MQTT exactly once — all before the thread exits.

#### Scenario: Inbound MMS with one image
- **WHEN** Twilio POSTs to `/api/messages` with `NumMedia=1`, `MediaContentType0=image/jpeg`, and a `MediaUrl0=https://api.twilio.com/...`
- **THEN** Flask responds 200/TwiML immediately (no `join()` on the background thread), spawns `_process_inbound_media_async`, and returns. The background thread downloads the bytes via Basic Auth, writes them to S3 under `media/images/{YYYY-MM}/media-{ISO-timestamp}.{ext}`, attaches a `media: [{"type": "image/jpeg", "url": "media/images/{YYYY-MM}/media-{ISO-timestamp}.jpg"}]` list to the resulting `Message.to_dict()`, and publishes the `MessageEnvelope` over MQTT exactly once.

#### Scenario: Inbound SMS without media
- **WHEN** Twilio POSTs to `/api/messages` with no `NumMedia` field (or `NumMedia=0`)
- **THEN** Flask proceeds exactly as today — synchronous, no background thread, no S3 upload, `Message.to_dict()` `media` field is `[]`.

#### Scenario: Inbound MMS with mixed content types (parallel uploads)
- **WHEN** Twilio POSTs to `/api/messages` with one `image/png` and one `video/mp4` attachment
- **THEN** Flask spawns the background thread which uses a `ThreadPoolExecutor` to download + upload both attachments in parallel. Each lands under its corresponding prefix (`media/images/...` and `media/videos/...`) and the final `media` list has two entries with the correct `type` strings.

#### Scenario: Twilio MediaUrl returns 410 GONE
- **WHEN** the inbound `MediaUrl*` HTTP fetch returns a non-2xx status (e.g., 410 because Twilio retention expired) OR fails to download within a configurable timeout (default 10s)
- **THEN** the background thread logs a WARNING, drops that media item from the list, and the message persists with the remaining items. A message whose media download fully fails still persists (text + empty `media` list) and publishes over MQTT.

#### Scenario: S3 upload fails after a successful Twilio fetch
- **WHEN** the bytes downloaded successfully but `boto3.put_object` raises (network, IAM, throttle)
- **THEN** the background thread logs a WARNING and the message persists with the affected media item dropped from the `media` list. Other media items in the same webhook still land. The original 200/TwiML response was already sent.

#### Scenario: Webhook response latency
- **WHEN** Twilio POSTs an MMS with 3 large attachments
- **THEN** Flask's 200/TwiML response returns in under 200 ms (wall clock) — none of the S3 / Twilio-MediaUrl / upload work blocks the request handler.

#### Scenario: MessageSid dedupe (Twilio retry)
- **WHEN** Twilio POSTs to `/api/messages` with the same `MessageSid` twice in quick succession (Twilio retry because it didn't see the first 200 fast enough)
- **THEN** the second call sees the SID in the in-process dedupe guard, returns 200/TwiML immediately without spawning a duplicate background thread. Only one `MessageEnvelope` is published.

#### Scenario: Background-thread crash leaves dedupe-guard released
- **WHEN** the background thread raises an unhandled exception
- **THEN** the `finally` block releases the dedupe-guard entry and logs CRITICAL; subsequent webhooks with the same `MessageSid` are processed normally (no deadlock).

#### Scenario: Inbound MMS with no body, only media
- **WHEN** Twilio POSTs to `/api/messages` with `Body=""` (or absent) and `NumMedia=1`
- **THEN** Flask accepts the message, responds 200/TwiML immediately, spawns the background thread; the thread persists a `Message` with `body=""` and a populated `media` list, then publishes the `MessageEnvelope` over MQTT. The 204 gate fires only when both `body` is empty AND `NumMedia == 0` (or absent).

#### Scenario: Empty body, no media still returns 204
- **WHEN** Twilio POSTs to `/api/messages` with `Body=""` and no `NumMedia` field
- **THEN** Flask returns 204 with no body, no background thread, no S3 writes, no MQTT publish — the same as today's behavior.

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

### Requirement: Flask returns a 302 to a signed S3 URL for authenticated media fetches
The Flask process MUST expose `GET /api/media/<path:s3_key>` for authenticated media fetches; on success the endpoint MUST return a `302 Found` response whose `Location` header points to a freshly-signed S3 URL. Bytes MUST NOT flow through Flask — the Pi and browser follow the redirect to S3 directly.

#### Scenario: Authenticated fetch returns 302 with Location header
- **WHEN** `GET /api/media/media/images/2025-12/media-2025-12-07T15-30-00Z.jpg` is called with the `X-API-Key` header
- **THEN** Flask calls `s3.signed_media_url(s3_key)` (which invokes `boto3.client("s3").generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)`) and returns `302 Found` with `Location: <signed-s3-url>`. The response body is empty; no S3 bytes are streamed through Flask.

#### Scenario: Pi or browser follows the redirect
- **WHEN** a client receives the 302 with `Location: https://{bucket}.s3.{region}.amazonaws.com/media/...?X-Amz-...`
- **THEN** the client issues a `GET` to the signed URL and receives the bytes directly from S3 with `Content-Type=image/jpeg` (the same MIME recorded at upload time). Flask is not on the bytes path.

#### Scenario: Unauthenticated fetch denied
- **WHEN** `GET /api/media/media/images/...` is called with no API key
- **THEN** Flask returns `401 Unauthorized` — no S3 signing call is attempted.

#### Scenario: Path traversal blocked
- **WHEN** `GET /api/media/../messages/foo.json` is called (the path contains `..`)
- **THEN** Flask returns `400 Bad Request` and does NOT call `s3.signed_media_url`.

#### Scenario: Missing key
- **WHEN** `GET /api/media/media/images/.../nonexistent.jpg` is called with valid auth and `s3.signed_media_url` raises (key does not exist)
- **THEN** Flask returns `404 Not Found`.

#### Scenario: S3 outage during signing
- **WHEN** `boto3.generate_presigned_url` raises (network, IAM, throttle)
- **THEN** Flask returns `502 Bad Gateway` with a JSON body `{ "error": "<message>" }` and logs a WARNING.

#### Scenario: Message with no body, only media
- **WHEN** `get_display_message()` returns the body of a message whose `body == ""` and `media` is non-empty
- **THEN** the coordinator's `out → in` transition constructs the `MediaCycler` (media is non-empty) AND calls `scroller.set_text("", display.width)` with `showing_text=False`. After the fade-in, the mode is `background` (not `hold`) — the cycler renders the media; the panel shows no text. The next transition (text_out is skipped because there's no text to fade) drops straight from `background` when `hold_seconds` elapses or the cycler's cycle ends, then advances to `self.effects[self.idx]` on the next fade.

#### Scenario: Cycle cutoff respects hold_seconds
- **WHEN** a `MediaCycler` is in active rendering mode and `hold_seconds` (configured to 15 s by default) elapses
- **THEN** the cycler stops advancing through its media list and the coordinator enters the existing `text_out → background` transition. The body of the message fades out; the rotation resumes on the next fresh-message or idle-trigger fade.

#### Scenario: MediaCycler for one short image
- **WHEN** a `MediaCycler` holds a single `image/jpeg` and `hold_seconds=15`
- **THEN** the image renders for the full `hold_seconds` window (15 s) — the cycler's "advance or hold" clock is gated by `hold_seconds`, not by an internal "all items shown" counter.

#### Scenario: MediaCycler item's natural duration read at construction
- **WHEN** a `MediaCycler` is constructed with a `video/mp4` item
- **THEN** it reads `cv2.CAP_PROP_FRAME_COUNT / cv2.CAP_PROP_FPS` once (at construction) to compute the item's natural duration; subsequent cycles reuse the cached value.

#### Scenario: Bad-codec item is dropped from the cycler's list
- **WHEN** the cycler's current item is a `video/mp4` whose inner `VideoDisplay` raises `cv2.error` (codec mismatch, can't read first frame) on `tick()`
- **THEN** the cycler logs a WARNING (`"MediaCycler: dropping item %r due to decode failure: %s"`), removes the item from its in-memory list, and advances to the next item on the next `tick()`. No black panel; no crash.

#### Scenario: All items bad-codec — fall back to rotation
- **WHEN** the cycler's only item (or every item) raises a decode failure on its first frame
- **THEN** the cycler's list becomes empty, and on the next fade the coordinator falls back to `self.effects[self.idx]` (the rotation). The sign continues to display the rotation's default effects (Flame, NightSky, Fireworks, etc.) — no dead black panel for the message's whole `hold_seconds` window.

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
