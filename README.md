---
title: Tag Validator Pro
emoji: 🏷️
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: true
---

# Tag Validator Pro

Bulk website tag validation tool with Omnibug-level CDP network interception.

Detects: **Tealium, Adobe Analytics, GTM, GA4** — including POST-based beacons and sendBeacon calls.

## Marketing Pixels — 4 consent scenarios + source

The **Marketing Pixels** mode loads every site under 4 OneTrust consent states —
**Necessary, Performance, Functional, Targeting** — injected via the
`OptanonConsent` cookie (banner accept/reject fallback for other CMPs). Click a
scenario tab to switch. For each pixel it shows the **fire count** and the
**source** that fired it (Tealium / Adobe / GTM / Hardcoded), attributed from the
JS request initiator stack via CDP. Compliance FAILs if any marketing pixel fires
under the Necessary (no-consent) scenario.

## Scheduling email alerts

When creating an automated schedule you can set an **alert email**. After every
scheduled run a summary + the list of failed websites is emailed with the full
Excel report attached. Email uses the **Brevo HTTP API** (sends over HTTPS, so it
works on hosts like Hugging Face Spaces that block SMTP ports).

Configure it in the app (Scheduler tab → Email Settings) or via env vars:

```
BREVO_API_KEY=xkeysib-your-api-key
BREVO_SENDER=verified-sender@example.com
```

Get a free API key at app.brevo.com → SMTP & API → API Keys. The sender email
must be a verified sender on your Brevo account (Senders & IP → Senders).
Without these the run still completes; only the email is skipped. Use **Send
Test Email** to verify setup.
