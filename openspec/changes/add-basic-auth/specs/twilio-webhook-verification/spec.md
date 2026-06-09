## ADDED Requirements

### Requirement: Twilio webhook signature verification

`POST /api/messages` SHALL verify Twilio's HMAC-SHA1 signature before processing. The signature is computed over the full raw POST body using `TWILIO_AUTH_TOKEN` as the key. If verification fails, the system SHALL return HTTP 403. If the signature is valid, the webhook SHALL be processed normally.

#### Scenario: Valid Twilio signature is accepted

- **WHEN** Twilio sends a `POST /api/messages` request with a valid `X-Twilio-Signature` header
- **THEN** the system verifies the signature using `TWILIO_AUTH_TOKEN` and processes the webhook normally (returns TwiML)

#### Scenario: Invalid Twilio signature returns 403

- **WHEN** a `POST /api/messages` request has an invalid or missing `X-Twilio-Signature` header
- **THEN** the system returns HTTP 403 and does not process the webhook
