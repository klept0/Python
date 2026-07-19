#!/usr/bin/env python3
"""
Internet Connection Monitor
----------------------------
Continuously checks internet connectivity. When the connection drops,
it logs the outage start. When the connection comes back, it logs the
outage and sends an Apprise notification with the downtime duration.

Setup:
    pip install apprise

Run:
    python connection_monitor.py

For long-term use, run it as a background service (see notes at the
bottom of this file for systemd / Task Scheduler / nohup options).
"""

import socket
import time
import logging
from datetime import datetime, timedelta

import apprise

# ---------------------------------------------------------------------------
# Configuration — edit these to fit your setup
# ---------------------------------------------------------------------------

CHECK_INTERVAL = 5          # seconds between connectivity checks
SOCKET_TIMEOUT = 3          # seconds to wait for a connection attempt

# Hosts used to test connectivity. Any one succeeding counts as "online".
# Using multiple hosts avoids false positives if one provider has a hiccup.
PING_HOSTS = [
    ("8.8.8.8", 53),   # Google DNS
    ("1.1.1.1", 53),   # Cloudflare DNS
    ("9.9.9.9", 53),   # Quad9 DNS
]

LOG_FILE = "connection_monitor.log"

# Apprise notification URL(s) — add one or more.
# Examples:
#   Discord:   discord://webhook_id/webhook_token
#   Telegram:  tgram://bot_token/chat_id
#   Pushover:  pover://user_key@app_token
#   Email:     mailtos://user:app_password@gmail.com
#   ntfy:      ntfy://ntfy.sh/your_topic
# Docs: https://github.com/caronc/apprise#popular-notification-services
APPRISE_URLS = [
    "ntfy://ntfy.sh/your_topic_here",
]

# Also send a notification the moment the connection drops (in addition
# to the "restored" notification)? Set True if you want both alerts.
NOTIFY_ON_DROP = False

# Print a "still alive" status line to the console/log at this interval
CONSOLE_HEARTBEAT_INTERVAL = 15 * 60      # 15 minutes

# Send an Apprise heartbeat notification at this interval, so you know
# the monitor itself hasn't silently died or lost power/network.
APPRISE_HEARTBEAT_INTERVAL = 24 * 60 * 60  # 24 hours

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

import sys

# On Windows, the console often defaults to a legacy codepage (e.g. cp1252)
# that can't render some characters. Force UTF-8 with a safe fallback so
# logging never crashes on an unusual character.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("conn_monitor")


def is_connected() -> bool:
    """Return True if any configured host is reachable."""
    for host, port in PING_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT):
                return True
        except OSError:
            continue
    return False


def format_duration(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def send_notification(title: str, body: str) -> None:
    apobj = apprise.Apprise()
    for url in APPRISE_URLS:
        apobj.add(url)

    if len(apobj) == 0:
        log.warning("No valid Apprise URLs configured — skipping notification.")
        return

    ok = apobj.notify(title=title, body=body)
    if not ok:
        log.error("Apprise failed to deliver the notification.")


def main() -> None:
    log.info("Starting internet connection monitor (interval: %ss)", CHECK_INTERVAL)

    connected = is_connected()
    log.info("Initial status: %s", "CONNECTED" if connected else "DISCONNECTED")
    outage_start = None if connected else datetime.now()

    # Heartbeat / stats tracking
    monitor_start = datetime.now()
    outage_count = 0
    total_downtime = timedelta()
    last_console_heartbeat = time.monotonic()
    last_apprise_heartbeat = time.monotonic()

    try:
        while True:
            time.sleep(CHECK_INTERVAL)
            now_connected = is_connected()

            if connected and not now_connected:
                # Connection just dropped
                outage_start = datetime.now()
                log.warning(
                    "Connection LOST at %s",
                    outage_start.strftime("%Y-%m-%d %H:%M:%S"),
                )
                if NOTIFY_ON_DROP:
                    send_notification(
                        "Internet Connection Lost",
                        f"Connection dropped at {outage_start.strftime('%Y-%m-%d %H:%M:%S')}",
                    )

            elif not connected and now_connected:
                # Connection just came back
                outage_end = datetime.now()
                duration = outage_end - outage_start
                msg = (
                    f"Connection restored: {outage_end.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Down since: {outage_start.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Total downtime: {format_duration(duration)}"
                )
                log.info(msg.replace("\n", " | "))
                send_notification("Internet Connection Restored", msg)
                outage_count += 1
                total_downtime += duration
                outage_start = None

            connected = now_connected

            # --- Console "still alive" heartbeat -----------------------
            now_mono = time.monotonic()
            if now_mono - last_console_heartbeat >= CONSOLE_HEARTBEAT_INTERVAL:
                uptime = datetime.now() - monitor_start
                log.info(
                    "[OK] Monitor alive | status: %s | running: %s | outages: %d | total downtime: %s",
                    "CONNECTED" if connected else "DISCONNECTED",
                    format_duration(uptime),
                    outage_count,
                    format_duration(total_downtime),
                )
                last_console_heartbeat = now_mono

            # --- Daily Apprise heartbeat ---------------------------------
            if now_mono - last_apprise_heartbeat >= APPRISE_HEARTBEAT_INTERVAL:
                uptime = datetime.now() - monitor_start
                heartbeat_msg = (
                    f"Monitor has been running for {format_duration(uptime)}.\n"
                    f"Current status: {'CONNECTED' if connected else 'DISCONNECTED'}\n"
                    f"Outages so far: {outage_count}\n"
                    f"Total downtime so far: {format_duration(total_downtime)}"
                )
                log.info("Sending daily heartbeat notification.")
                send_notification("Connection Monitor Heartbeat", heartbeat_msg)
                last_apprise_heartbeat = now_mono

    except KeyboardInterrupt:
        log.info("Monitor stopped by user.")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Running this persistently
# ---------------------------------------------------------------------------
# Linux (systemd) — create /etc/systemd/system/conn-monitor.service:
#
#   [Unit]
#   Description=Internet Connection Monitor
#   After=network.target
#
#   [Service]
#   ExecStart=/usr/bin/python3 /path/to/connection_monitor.py
#   Restart=always
#   WorkingDirectory=/path/to/
#
#   [Install]
#   WantedBy=multi-user.target
#
# Then: sudo systemctl enable --now conn-monitor
#
# Linux/Mac (quick and dirty): nohup python3 connection_monitor.py &
#
# Windows: use Task Scheduler with trigger "At log on", action running
# pythonw.exe connection_monitor.py, or wrap it as a service with NSSM.
