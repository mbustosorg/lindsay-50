# Hot-patch handoff — config publish to /testing not propagating

**Session that produced this:** `orchestrator/l5-orchestrator` worktree,
branch `orchestrator/l5-orchestrator`, ran out of context. Picking up
in a fresh session on the `lindsay-50` worktree.

## Worktree

- Repo root: `/Users/adam/dev/GitHub/lindsay-50/`
- Branch: `main` (currently at `3d8cca3`)
- Heroku slug: **v169** (deployed 2026-07-20 ~15:58 PT, contains `?v=15` shim)

## The bug

`/settings` save does NOT propagate to already-open `/testing` tab.
Flask publishes a `config` envelope on `mbustosorg/feeds/lindsay50`
via `lib_shared/paho_mqtt_client.py:publish_envelope()`. Browser WS
on /testing is subscribed at QoS 1 to the same topic. After 20s the
DOM on /testing does not update.

## What we KNOW

Three independent observations all confirm the broker is **NOT
fanning the config envelope out to subscribers** (confirmed against
Heroku v169 with `?v=15` deployed):

1. **Browser WS (`?v=15` ingest log)**: in a 15s window between
   `POST /settings return` and `wait-complete`, the WS subscriber
   receives exactly three frames and NOTHING else:
   - `20 02 00 00` = CONNACK
   - `90 03 00 01 01` = SUBACK (QoS 1)
   - `d0 00` = PINGRESP (keepalive)
   No PUBLISH frame ever arrives on the config topic.

2. **Heroku Flask logs** (1000+ lines searched): zero
   `[MQTT_INCOMING] topic=mbustosorg/feeds/lindsay50` records
   (excluding `-status`). Flask's own paho subscriber on the same
   connection never receives its own publish. **Zero fanned-out
   copies exist for any subscriber on that feed.**

3. **Mac publish from local**: `paho clean_session=True` publish to
   the SAME topic from this Mac → WS subscriber receives it fine.
   So the broker CAN fan out for some publishers, just not for
   Heroku's Flask.

4. **Status feed works**: `mbustosorg/feeds/lindsay50-status` (Pi → Flask)
   fans out fine every 5s, appearing twice in Flask logs (one copy
   for the paho subscriber, one for the browser's WS bridge).
   So the fan-out machinery itself works on AIO; the failure is
   specific to the config feed.

## What we DON'T know

- WHY AIO broker accepts the publish (PUBACK) but doesn't fan it
  out. Could be:
  - AIO free-tier per-feed subscriber-cap hit
  - AIO feed-level throttling / state stuck
  - Heroku dyno IP rate-limited differently than consumer connections
  - Per-publisher (Heroku) publish ACL restricted on that specific feed
- Whether the issue reproduces for an arbitrary other feed (sanity
  check: try publishing to a NEW feed like `lindsay50-test` from
  Heroku → WS subscribe there → does it fan out?)

## What we TRIED that did NOT work

| Attempt | What it changed | Result |
|---|---|---|
| `d71095b` `clean_session=False` + stable client_id + 2s pre-disconnect sleep | Per-publish paho client held a persistent session for 2s after publish | Heroku logs: zero `[MQTT_INCOMING]` on config topic — broker accepts PUBACK but does not fan out. Hypothesis test passed from Mac, broke on Heroku. |
| `c7f670e` Bumped pre-disconnect sleep 2s → 5s | Same approach, longer hold | Same result: zero fan-out. |
| `2477486` Revert both | Back to `clean_session=True` short-lived publisher | Baseline behavior — bug unchanged but not worsened. **Current state.** |

## Commits on `main` (oldest to newest, relevant subset)

```
3d8cca3 chore(heroku): touch main.py to force slug rebuild
f04c99e diag: dump raw payload before parsing (?v=15)
9f2e051 diag(mqtt-ws): dump full raw payload before any parsing (?v=15)
1c0c33f diag: ingest-chunk entry-point log (?v=14)
f9a2d2a diag(mqtt-ws): add ingest-chunk entry-point log (?v=14)
1fe7c73 revert: drop round-7a clean_session=False attempt
2477486 revert(paho): drop clean_session=False attempt — broken on Heroku
e2f1d06 merge: bump post-publish hold 2s -> 5s for Heroku AIO fan-out
c7f670e fix(paho): bump post-publish hold 2s -> 5s for Heroku latency margin
d71095b fix(paho): persistent session so AIO fans config publish to subscribers
```

(Note: the d71095b / c7f670e fix-and-bump are still in history because
they were merged into main before being reverted; they're effectively
dead code. The revert `2477486` is what actually ships in v169.)

## Diagnostic instrumentation LIVE in v169

`heart-message-manager/static/mqtt_ws_client.js` (`?v=15`):
- `ingest()` logs raw payload bytes BEFORE any parsing (full UTF-8
  decode, every chunk)
- `handleFrame()` logs every frame type + first 8 bytes hex
- PUBLISH-typed frames log parsed topic + JSON type/version

`heart-message-manager/templates/base.html` cache-buster bumped to
`?v=15`. app.js still at `?v=16`.

## Memory entries saved this session

- `feedback_clean_session_aio_fan_out.md`: clean_session=False is NOT
  an AIO fan-out lever on Heroku; reverted.
- `feedback_no_fundamental_changes_without_approval.md`: ASK before
  changing sessionStorage cache / single-flight gates / retry
  cadence.

## Files to investigate next

- `lib_shared/paho_mqtt_client.py` — `publish_envelope()` is the
  single publish site for both config and message envelopes; same
  code path for both, so the difference is NOT in publisher code.
- `lib_shared/adafruit_mqtt_client.py` — alternative publisher used
  in Heroku production (the Adafruit IO native client). Could swap
  `paho_mqtt_client` for this and see if fan-out differs. But Heroku
  config (`MQTT_CLIENT = "adafruit"`) is probably the default already.
- `heart-message-manager/main.py:354-358` — the `publish_envelope`
  call site for the message envelope (line 356). The user confirmed
  a test message from /testing DOES propagate to the browser WS —
  so this code path works. The config path is identical (line 254).

## Heroku slug gotcha

`heroku/main` git ref can drift from the actual deployed slug. The
v169 rebuild was forced by a no-op trailing newline in
`heart-message-manager/main.py` (commit `3d8cca3`). When you push,
check `heroku releases -a lindsay-50` and the served HTML's
`?v=N` value to confirm what's actually deployed — `git push heroku
main` reports "Everything up-to-date" if the SHA matches the
tracking ref even when no rebuild happened.

## Likely next investigation

Try publishing to a DIFFERENT AIO feed from Heroku Flask, see if
fan-out works on a fresh feed. If yes → AIO feed state stuck on
`lindsay50`. If no → broader broker-side or Heroku-IP issue.

Worth asking Adafruit IO support with the evidence bundle:
- Feed: `mbustosorg/feeds/lindsay50`
- Symptom: publish accepted (PUBACK), zero fan-out to any subscriber
- Mirror publish from Mac works fine on same feed
- Sibling feed `mbustosorg/feeds/lindsay50-status` works fine

## Heroku credentials (settings.toml)

In the worktree, `heart-message-manager/settings.toml` is gitignored.
The values needed to point at the live Heroku app:
- `BASE_URL`: `https://lindsay-50-c36202ae5ca0.herokuapp.com`
- Admin user: `admin` / `password123` (in `.env` on the dyno)
- `MQTT_USERNAME`: `mbustosorg`
- `MQTT_PASSWORD`: `cdce8cac75517a93a5fcb52c0b8213a6469127e6`
- `MQTT_TOPIC`: `mbustosorg/feeds/lindsay50`
- `MQTT_STATUS_TOPIC`: `mbustosorg/feeds/lindsay50-status`