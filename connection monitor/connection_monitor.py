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

import os
import socket
import time
import json
import logging
import signal
import argparse
import urllib.request
import urllib.error
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta

import apprise

# ---------------------------------------------------------------------------
# Configuration — edit these to fit your setup
# ---------------------------------------------------------------------------

CHECK_INTERVAL = 5          # seconds between connectivity checks (normal)
SOCKET_TIMEOUT = 3          # seconds to wait for a connection attempt

# How many CONSECUTIVE failed checks are required before an outage is
# declared. Guards against a single blip (busy DNS server, one dropped
# packet) triggering a false "down" -> "restored" notification pair.
# A single successful check always ends an outage immediately.
FAILURE_THRESHOLD = 2

# Once an outage has lasted this long, slow the check interval down to
# OUTAGE_CHECK_INTERVAL. No point hammering the network every 5s during
# a multi-hour ISP outage; this resets back to CHECK_INTERVAL as soon as
# the connection returns.
BACKOFF_AFTER_SECONDS = 60
OUTAGE_CHECK_INTERVAL = 30

# Hosts used to test connectivity. Any one succeeding counts as "online".
# Using multiple hosts avoids false positives if one provider has a hiccup.
PING_HOSTS = [
    ("8.8.8.8", 53),   # Google DNS
    ("1.1.1.1", 53),   # Cloudflare DNS
    ("9.9.9.9", 53),   # Quad9 DNS
]

LOG_FILE = "connection_monitor.log"
LOG_RETENTION_DAYS = 7      # how many rotated daily log files to keep

# A separate log containing ONLY completed downtime events (one line per
# outage: start, end, duration) — nothing else gets written here. This
# one isn't rotated; it's small and worth keeping as a permanent record.
DOWNTIME_LOG_FILE = "downtime.log"

# Only one instance of the monitor should run at a time (e.g. avoids
# duplicate notifications if it's launched both at login and manually).
# Enforced by binding to this local-only port; harmless, never exposed.
SINGLE_INSTANCE_LOCK_PORT = 47563

# Apprise notification URL(s) — loaded from an external file (or env var)
# rather than hardcoded here, since these often contain real secrets
# (email app passwords, API tokens, webhook IDs). Keeping them out of the
# script means they never end up in git history if this file is tracked.
#
# Resolution order:
#   1. apprise_urls.txt next to this script — one URL per line, '#' comments OK
#   2. APPRISE_URLS environment variable — comma-separated
#   3. _FALLBACK_APPRISE_URLS below (only used if neither of the above exists)
#
# Examples of valid URLs:
#   Discord:   discord://webhook_id/webhook_token
#   Telegram:  tgram://bot_token/chat_id
#   Pushover:  pover://user_key@app_token
#   Email:     mailtos://user:app_password@gmail.com
#   ntfy:      ntfy://ntfy.sh/your_topic
# Docs: https://github.com/caronc/apprise#popular-notification-services
APPRISE_URLS_FILE = "apprise_urls.txt"
_FALLBACK_APPRISE_URLS = [
    "ntfy://ntfy.sh/your_topic_here",
]


def _load_apprise_urls() -> list[str]:
    if os.path.exists(APPRISE_URLS_FILE):
        with open(APPRISE_URLS_FILE, "r") as f:
            urls = [
                line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
        if urls:
            return urls

    env_urls = os.environ.get("APPRISE_URLS", "")
    if env_urls:
        return [u.strip() for u in env_urls.split(",") if u.strip()]

    return _FALLBACK_APPRISE_URLS


APPRISE_URLS = _load_apprise_urls()

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
        TimedRotatingFileHandler(
            LOG_FILE, when="midnight", backupCount=LOG_RETENTION_DAYS, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("conn_monitor")

# Dedicated downtime-only logger — separate file, separate handler, and
# propagate=False so these lines never also land in the main log/console.
downtime_log = logging.getLogger("conn_monitor.downtime")
downtime_log.setLevel(logging.INFO)
downtime_log.propagate = False
_downtime_handler = logging.FileHandler(DOWNTIME_LOG_FILE, encoding="utf-8")
_downtime_handler.setFormatter(logging.Formatter("%(message)s"))
downtime_log.addHandler(_downtime_handler)


def acquire_instance_lock() -> "socket.socket | None":
    """
    Ensure only one copy of the monitor runs at a time. Binds a local-only
    TCP port; if that fails, another instance already holds it. The OS
    releases the port automatically when this process exits (even on a
    crash), so there's no stale lock file to clean up.
    """
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lock_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        lock_socket.bind(("127.0.0.1", SINGLE_INSTANCE_LOCK_PORT))
        lock_socket.listen(1)
        return lock_socket
    except OSError:
        lock_socket.close()
        return None


class ShutdownSignal(Exception):
    """Raised when the process receives a termination signal (e.g. SIGTERM)."""


def _handle_termination_signal(signum, frame):
    raise ShutdownSignal(signal.Signals(signum).name)


def _install_signal_handlers() -> None:
    # SIGTERM: sent by systemd on `stop`, Docker, `kill <pid>`, etc.
    # Windows doesn't reliably deliver this on Task Scheduler kills, but
    # it's harmless to register and does work for e.g. WSL/Linux setups.
    try:
        signal.signal(signal.SIGTERM, _handle_termination_signal)
    except (AttributeError, ValueError):
        pass


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


_PLACEHOLDER_MARKERS = ("your_topic_here", "webhook_id", "webhook_token", "user_key@app_token")


def _check_apprise_urls_configured() -> None:
    """Warn at startup if APPRISE_URLS still contains the example placeholder."""
    if not APPRISE_URLS:
        log.warning("APPRISE_URLS is empty — no notifications will be sent.")
        return
    for url in APPRISE_URLS:
        if any(marker in url for marker in _PLACEHOLDER_MARKERS):
            log.warning(
                "APPRISE_URLS still contains a placeholder value (%s) — "
                "replace it with your real notification URL or nothing will be delivered.",
                url,
            )


def send_notification(title: str, body: str) -> None:
    apobj = apprise.Apprise()

    added_any = False
    for url in APPRISE_URLS:
        added = apobj.add(url)
        if not added:
            log.error("Apprise rejected this URL (bad format?): %s", url)
        else:
            added_any = True

    if not added_any:
        log.warning("No valid Apprise URLs configured — skipping notification.")
        return

    try:
        ok = apobj.notify(title=title, body=body)
    except Exception:
        log.exception("Apprise raised an exception while sending notification '%s'", title)
        return

    if ok:
        log.info("Notification sent: %s", title)
    else:
        log.error(
            "Apprise reported failure delivering '%s' — check the URL, "
            "network access to the notification service, and any API "
            "keys/tokens involved.",
            title,
        )


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
    lock = acquire_instance_lock()
    if lock is None:
        log.error(
            "Another instance of the monitor appears to already be running "
            "(port %d is in use) — exiting.",
            SINGLE_INSTANCE_LOCK_PORT,
        )
        return
    _install_signal_handlers()

    log.info("Starting internet connection monitor (interval: %ss)", CHECK_INTERVAL)
    _check_apprise_urls_configured()

    connected = is_connected()
    log.info("Initial status: %s", "CONNECTED" if connected else "DISCONNECTED")
    outage_start = None if connected else datetime.now()
    consecutive_failures = 0

    send_notification(
        "Connection Monitor Started",
        f"Monitor started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.\n"
        f"Initial status: {'CONNECTED' if connected else 'DISCONNECTED'}",
    )

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
            # Back off the check rate during a long-confirmed outage —
            # no need to hammer the network every 5s for hours on end.
            sleep_for = CHECK_INTERVAL
            if not connected and outage_start is not None:
                if (datetime.now() - outage_start).total_seconds() >= BACKOFF_AFTER_SECONDS:
                    sleep_for = OUTAGE_CHECK_INTERVAL
            time.sleep(sleep_for)

            raw_connected = is_connected()
            if raw_connected:
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            if connected and consecutive_failures >= FAILURE_THRESHOLD:
                # Confirmed down (not just a single blip)
                outage_start = datetime.now()
                log.warning(
                    "Connection LOST at %s (after %d consecutive failed checks)",
                    outage_start.strftime("%Y-%m-%d %H:%M:%S"),
                    consecutive_failures,
                )
                if NOTIFY_ON_DROP:
                    send_notification(
                        "Internet Connection Lost",
                        f"Connection dropped at {outage_start.strftime('%Y-%m-%d %H:%M:%S')}",
                    )
                connected = False

            elif not connected and raw_connected:
                # A single successful check is enough to confirm restoration
                outage_end = datetime.now()
                duration = outage_end - outage_start
                msg = (
                    f"Connection restored: {outage_end.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Down since: {outage_start.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Total downtime: {format_duration(duration)}"
                )
                log.info(msg.replace("\n", " | "))
                downtime_log.info(
                    "Down: %s  ->  %s  |  Duration: %s",
                    outage_start.strftime("%Y-%m-%d %H:%M:%S"),
                    outage_end.strftime("%Y-%m-%d %H:%M:%S"),
                    format_duration(duration),
                )
                send_notification("Internet Connection Restored", msg)
                outage_count += 1
                total_downtime += duration
                outage_start = None
                connected = True

                # IP can change while offline (DHCP re-lease, ISP hiccup,
                # router reboot, power outage) — check right away.
                last_ip = check_for_ip_change(last_ip, context="after reconnect")
                last_ip_check = time.monotonic()

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
        log.info("Monitor stopped by user (Ctrl+C).")
    except ShutdownSignal as e:
        log.info("Monitor stopped by system signal (%s).", e)
    finally:
        lock.close()
        log.info("Monitor exiting.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Internet connection monitor with Apprise notifications.")
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="Send a single test Apprise notification using APPRISE_URLS, then exit "
             "(does not start the monitor or require the instance lock).",
    )
    args = parser.parse_args()

    if args.test_notify:
        log.info("Running notification test...")
        _check_apprise_urls_configured()
        send_notification(
            "Connection Monitor Test",
            f"This is a test notification sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. "
            f"If you received this, Apprise is configured correctly.",
        )
    else:
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
