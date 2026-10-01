# Stable quota observations

ChatGPT Usage 0.1.4 keeps existing account and entity identifiers.

## Credit history

Credit balance and reset credits are numeric measurement sensors. Home Assistant
shows their history as line charts and records long-term minimum, maximum, and
mean values. Both default to whole-number display; the credit balance retains
the provider's full precision in recorded values. Missing balances remain unknown.

Existing entity IDs and Recorder history are preserved. Long-term statistics
start after upgrading to 0.1.3; the integration does not backfill old statistics.
Credits, banked resets, and credit availability are primary account information,
not diagnostics. Missing or malformed credit flags produce unknown availability.

## Native account status

**Account status** is an enum sensor with four states: Available, Credits available,
Limit reached, and Account blocked. Incomplete quota data produces Unknown; polling
failure produces Unavailable. Its history records the status shown on the dashboard.

Credits available means the provider reports credit entitlement after ordinary
allowance is exhausted. It does not spend credits, apply a banked reset, or claim
that a blocked account can run a request. An explicit spending blocker takes
precedence. The separate ordinary allowance binary sensor drives recovery detection.

## What the times mean

- **Reset** is the provider's next scheduled deadline. Changes of up to five seconds
  retain the previous timestamp to avoid recording one-second polling jitter.
- When usage is zero and the reported deadline is a full window into the future,
  **Reset** is unknown with `reset_status: awaiting_usage`. This avoids publishing
  a deadline that advances on every poll while the account is idle. A deadline
  reported after activity begins is displayed normally.
- **Last reset** is the time Home Assistant observed an allowance refill. It is
  not the provider's scheduled time. Each window has its own sensor; the account
  sensor shows the most recent ordinary Codex window refill.
- On upgrade, the integration reads up to 14 days of existing Recorder usage history
  to recover prior observed refills. It does not invent a time when history is absent,
  and it does not send notifications for imported history.
- Reset observations persist through restarts. A moved deadline alone, without a
  usage decrease, does not count as an observed reset.
- Known reset and recovery timestamps remain available during polling failures.
  They remain observations from the past, while live quota and account status
  become unavailable until a successful poll.
- Countdown sensors are optional diagnostics, disabled by default for new entities.
  Existing enabled entities stay enabled for compatibility. Use the reset timestamp
  with a relative display for a countdown that does not depend on polling frequency.

## Account recovery

**Ordinary allowance available** (formerly **Usable**) refers to ordinary Codex
allowance. Every reported main window must be usable, with no reported credit or
spending blocker. **Ordinary allowance limit reached** (formerly **Limit reached**)
now uses the same main-window limit rules, including `allowed: false`. Additional
feature or model limits remain visible through their own window entities; they do
not imply that ordinary Codex usage is blocked. Existing entity IDs stay unchanged.
Missing data is unknown. An exhausted main window disappearing from a response
does not count as recovery.

The `chatgpt_usage_account_recovered` event fires after an observed transition
from limited/blocked to available. Its payload contains `entry_id`, `entry_title`,
`observed_at`, `remaining_percent`, and the reported main `windows`.

The previous availability and pending startup events are persisted. Events wait
until Home Assistant has started so automations can receive them. A normal restart,
first healthy reading, or connection failure followed by the same healthy state
does not produce a recovery alert. Notification delivery remains the automation's
responsibility; the integration does not claim receipt on the phone.

Each account also has **Reset events** and **Recovery events** entities. Reset events
record ordinary allowance refills; Recovery events record usable-again transitions.
Use HA's `event.received` trigger with event type `recovered` and `mode: queued` so
recoveries from different accounts do not cancel or suppress each other. In the
notification action, the payload is in `trigger.to_state.attributes`, including
`entry_id`, `observed_at`, and `remaining_percent`.

The original bus events remain supported. Reset bus events now include `is_main`
and still report additional feature refills. Avoid listening to both the bus event
and event entity in one notification workflow, which would deliver twice.

Native event history begins with the next observed event after installation. The
integration never emits a fake event to backfill history. Existing timestamp sensors
retain older observations. Event entities restore their last event on reload without
triggering a fresh recovery. The event state is HA's delivery timestamp; `observed_at`
is the original observation time, including for events queued during startup.

## Polling

Five minutes is the default and minimum interval in Options. Existing intervals remain
unchanged until updated through the integration's Options. Provider rate-limit
backoff still takes precedence. The observed time can therefore be later than
the actual provider reset.

The integration also supports HA's standard polling controls: disable polling in
the account entry's **System options**, then call `homeassistant.update_entity`
from an automation with your chosen schedule. Target one usage sensor per account;
the shared coordinator updates all of that account's entities. See
[Polling](../README.md#polling) for the configuration steps and naming conventions.

The manual refresh button stays available after failed polls. It retries through
the coordinator and respects provider rate-limit backoff.

## Release and rollback

Run the Testmon suite and Ruff before publishing. The HA runtime job covers the
deployed 2026.9.4 version. Build the HACS artifact with:

```sh
python scripts/build_release.py
```

Publish `dist/chatgpt_usage.zip` with the version tag. It contains only the
`chatgpt_usage` component, while the repository retains the separate Claude code.
HACS installs the archive into `/config/custom_components/chatgpt_usage`.

When switching from the upstream HACS repository, remove only its downloaded
package through HACS, then install this fork before restarting. Preserve the
three integration config entries; deleting those also deletes their entity registry
records. Save the dashboard and automation definitions before changing them.

To roll back the package, reinstall the previously working fork release through
HACS and restart HA. Restore any changed dashboard or automation through its config API.
The stored reset-state schema keeps the original fields and adds optional fields,
so the older integration can still read it.
