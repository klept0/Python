# Internet Connection Monitor

Watches your internet connection, logs outages, tracks your external IP,
and notifies you via [Apprise](https://github.com/caronc/apprise) when
things change.

## Features

- Detects outages and restorations, with a 2-consecutive-failure threshold
  to avoid false alarms from a single dropped check
- Logs every completed outage to `downtime.log` (start, end, duration)
- Sends an Apprise notification when the connection is restored
- Detects external IP changes (on startup, right after any reconnect, and
  every 15 min while connected) and notifies if it changes
- Console "still alive" status every 15 min + one daily Apprise heartbeat
- Backs off its check interval during long outages instead of polling
  every 5 seconds for hours
- Refuses to run two copies at once
- Rotates its main log daily, keeping 7 days

## Setup

```bash
pip install apprise
```

Create `apprise_urls.txt` in the same folder as the script — one
notification URL per line, `#` for comments. See the file for examples
(Discord, Telegram, Pushover, email, ntfy, etc.). Full URL format docs:
https://github.com/caronc/apprise#popular-notification-services

Alternatively, set the `APPRISE_URLS` environment variable
(comma-separated) instead of using the file.

Without either, the monitor still runs and logs normally — it just won't
send any notifications, and it'll warn you about that on startup.

## Usage

```bash
# Start monitoring
python connection_monitor.py

# Test your Apprise setup without starting the monitor
python connection_monitor.py --test-notify
```

Stop with `Ctrl+C`.

## Files it creates

| File | Contents |
|---|---|
| `connection_monitor.log` | Full activity log (rotates daily, 7-day retention) |
| `downtime.log` | One line per completed outage — nothing else |
| `last_known_ip.json` | Last-seen external IP, so changes are still caught after a restart |
| `apprise_urls.txt` | Your notification URLs (you create this — keep it out of git) |

## Configuration

All settings are constants near the top of the script — check interval,
failure threshold, heartbeat intervals, IP check interval/services, log
retention, and the single-instance lock port.

## Running it long-term

See the comment block at the bottom of `connection_monitor.py` for
systemd (Linux), `nohup` (Linux/Mac), and Task Scheduler (Windows)
setup notes. Whatever runs it needs its working directory set to the
script's folder, since `apprise_urls.txt` and the log/state files use
relative paths.

## Security note

`apprise_urls.txt` can contain real secrets (app passwords, API tokens).
Don't commit it — add it to `.gitignore` (a starter one is included).
