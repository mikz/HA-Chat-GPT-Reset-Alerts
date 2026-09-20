# Stable quota observations

ChatGPT Usage 0.1.2 keeps existing account and entity identifiers.

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

## Account recovery

**Usable** refers to ordinary Codex allowance. Every reported main window must be
usable, with no reported credit or spending blocker. Additional feature or model
limits remain visible through their own entities and the existing aggregate
**Limit reached** sensor; they do not imply that ordinary Codex usage is blocked.
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

Use `mode: queued` for a notification automation listening to this event so
recoveries from different accounts do not cancel or suppress each other. Keep
`chatgpt_usage_reset` only for automations that want individual window refills.

## Polling

Five minutes is the default and minimum interval. Existing intervals remain
unchanged until updated through the integration's Options. Provider rate-limit
backoff still takes precedence. The observed time can therefore be later than
the actual provider reset.

## Release and rollback

Run the Testmon suite and Ruff before publishing. The HA runtime job covers the
deployed 2026.9.2 version. Build the HACS artifact with:

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

To roll back the package, reinstall the prior upstream commit through HACS and
restart HA. Restore the previous dashboard and automation through their config APIs.
The stored reset-state schema keeps the original fields and adds optional fields,
so the older integration can still read it.
