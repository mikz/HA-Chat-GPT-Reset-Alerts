# Claude Usage for Home Assistant

Monitor Claude subscription allowances, model-specific limits, and Extra Usage.
This package is separate from ChatGPT Usage and can be installed alongside it.

## Install

1. Add `mikz/HA-Claude-Usage` as a custom Integration repository in HACS.
2. Install Claude Usage and restart Home Assistant.
3. Open Settings > Devices & services > Add Integration > Claude Usage.
4. Choose Remote Anthropic, follow the authorization link, and paste the returned
   code into Home Assistant. The flow requests `user:profile` access, not inference.

Account names use the email address returned by Anthropic. Each account needs its
own authorization. The local Windows helper remains supported by the source project.

## Usage and history

Usage and remaining allowance are numeric percentage sensors. Extra Usage spend,
remaining budget, and limit retain full numeric precision. When Anthropic reports
a currency, these use Home Assistant's monetary device class and two decimal places
for display. Monetary balances use ordinary Recorder history rather than cumulative
statistics. Unknown values stay unknown.

Account status distinguishes ordinary allowance from paid Extra Usage. A model-specific
limit does not mark every model unavailable; its own usage sensors show that limit.
Missing exhausted quota windows cannot count as recovery.

Reset deadlines ignore changes of up to five seconds. An unused full window whose
deadline moves with the clock shows Awaiting new usage. Last reset records when
Home Assistant observed a refill. These observations survive reloads and restarts.
Native Reset events and Recovery events entities provide history. Recovery means
ordinary allowance became usable again; neither extra spending nor timestamp noise
causes a recovery notification.

Known reset times and manual refresh remain available during temporary failures.
Live usage and availability become unavailable until a successful poll.
The provider's rate-limit backoff applies to manual refresh as well.

Claude's current usage schema does not report OpenAI-style banked resets or their
expiry dates. The integration does not manufacture those values.

## Polling

Configure **Polling interval (seconds)** in the integration options, from 300 to
14,400 seconds. New entries default to 300 seconds; existing entries keep their option.

For a Home Assistant schedule, disable polling in the entry's System options and
call `homeassistant.update_entity` on one usage sensor. Every account uses one shared
coordinator, so one update refreshes all its entities.

## Dashboard and notifications

Link displayed values to their underlying entities so clicking opens details and
history. Show absolute local dates with relative time. Keep the refresh button beside
status, and use one shared history graph to show allowance draining and refilling.

Use the Recovery events entity with Home Assistant's `event.received` trigger and
event type `recovered`. The integration persists deduplication before dispatch and
waits for Home Assistant startup, so restarting does not replay old notifications.

## Source and updates

The source and full test suite live in
[mikz/HA-Chat-GPT-Reset-Alerts](https://github.com/mikz/HA-Chat-GPT-Reset-Alerts).
`SOURCE.json` records the exact source revision of this HACS package.

From a tested, committed source checkout, run:

```sh
python scripts/export_claude.py /path/to/HA-Claude-Usage
python scripts/build_release.py --domain claude_usage
```

Commit the exported repository and publish `dist/claude_usage.zip` as a release
asset there. HACS then handles installation and updates independently of OpenAI.
Before a live update, save the affected dashboard and automation definitions, check
the deployed HA version, and verify the release artifact against the tested source.
Keep existing config entries to preserve entity IDs and Recorder history.
To roll back, install the previous release through HACS, restart, and restore only
affected dashboard or automation objects through their config APIs.

This is an unofficial integration using Anthropic usage endpoints that can change.
It is not affiliated with Anthropic. See LICENSE and NOTICE.md for attribution.
