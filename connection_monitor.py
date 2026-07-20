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
import json
import logging
import urllib.request
import urllib.error
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

# --- External IP change detection -------------------------------------
# How often to re-check your external IP while connected (in addition to
# the check performed on startup and immediately after any reconnect).
IP_CHECK_INTERVAL = 15 * 60   # 15 minutes

# Services to query for your external IP, tried in order until one works.
IP_CHECK_SERVICES = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

# Where the last-known IP is persisted, so a change is still detected
# even if the script itself restarts (e.g. after a power outage / reboot).
IP_STATE_FILE = "last_known_ip.json"

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


def get_external_ip() -> str | None:
    """Query external services for the current public IP. Returns None on failure."""
    for url in IP_CHECK_SERVICES:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    return ip
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
    return None


def load_last_ip() -> str | None:
    """Load the last-known external IP from disk, if it exists."""
    try:
        with open(IP_STATE_FILE, "r") as f:
            data = json.load(f)
            return data.get("ip")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_last_ip(ip: str) -> None:
    """Persist the current external IP to disk."""
    try:
        with open(IP_STATE_FILE, "w") as f:
            json.dump({"ip": ip, "updated": datetime.now().isoformat()}, f)
    except OSError as e:
        log.error("Could not save IP state file: %s", e)


def check_for_ip_change(last_ip: str | None, context: str = "") -> str | None:
    """
    Check the current external IP against the last-known one.
    Logs and sends an Apprise notification if it changed.
    Returns the IP that should now be treated as "last known"
    (the current IP if the check succeeded, otherwise the unchanged last_ip).
    """
    current_ip = get_external_ip()
    if current_ip is None:
        log.warning("Could not determine external IP (all lookup services failed).")
        return last_ip

    if last_ip is None:
        log.info("External IP: %s", current_ip)
        save_last_ip(current_ip)
        return current_ip

    if current_ip != last_ip:
        log.warning("External IP changed: %s -> %s%s", last_ip, current_ip,
                     f" ({context})" if context else "")
        send_notification(
            "External IP Address Changed",
            f"Previous IP: {last_ip}\n"
            f"New IP: {current_ip}\n"
            f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            + (f"\nContext: {context}" if context else ""),
        )
        save_last_ip(current_ip)
        return current_ip

    return current_ip


def main() -> None:
    log.info("Starting internet connection monitor (interval: %ss)", CHECK_INTERVAL)

    connected = is_connected()
    log.info("Initial status: %s", "CONNECTED" if connected else "DISCONNECTED")
    outage_start = None if connected else datetime.now()

    # External IP tracking — load whatever was last seen (survives restarts),
    # then check it now if we're online.
    last_ip = load_last_ip()
    if connected:
        last_ip = check_for_ip_change(last_ip, context="startup")
    last_ip_check = time.monotonic()

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

                # IP can change while offline (DHCP re-lease, ISP hiccup,
                # router reboot, power outage) — check right away.
                last_ip = check_for_ip_change(last_ip, context="after reconnect")
                last_ip_check = time.monotonic()

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

            # --- Periodic external IP check ------------------------------
            if connected and (now_mono - last_ip_check >= IP_CHECK_INTERVAL):
                last_ip = check_for_ip_change(last_ip, context="routine check")
                last_ip_check = now_mono

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
