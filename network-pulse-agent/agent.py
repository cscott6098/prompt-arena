#!/usr/bin/env python3
"""Network Pulse monitoring agent.

Runs on-site (Raspberry Pi), checks gateway + internet reachability on a
fixed interval, and reports every cycle to Supabase as a heartbeat. Debounces
its own online/offline determination locally (so devices.current_status is
meaningful on the dashboard) but does not write status_events or send
alerts -- that ownership is left to a Supabase Edge Function / scheduled job.
"""

import datetime
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE .env file, without overriding
    variables that are already set (e.g. by systemd's EnvironmentFile=)."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(SCRIPT_DIR / ".env")


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required config: {name}")
    return value


SUPABASE_URL = env("SUPABASE_URL", required=True).rstrip("/")
SUPABASE_KEY = env("SUPABASE_KEY", required=True)
DEVICE_ID = env("DEVICE_ID", required=True)
CLIENT_ID = env("CLIENT_ID", "")  # informational label only; not sent to Supabase

CHECK_INTERVAL_SECONDS = int(env("CHECK_INTERVAL_SECONDS", "30"))
FAILURE_THRESHOLD = int(env("FAILURE_THRESHOLD", "3"))
PING_TIMEOUT_SECONDS = int(env("PING_TIMEOUT_SECONDS", "2"))

GATEWAY_IP = env("GATEWAY_IP", "").strip() or None
EXTERNAL_TARGETS = [
    t.strip() for t in env("EXTERNAL_TARGETS", "1.1.1.1,8.8.8.8").split(",") if t.strip()
]

LOG_FILE = env("LOG_FILE", str(SCRIPT_DIR / "agent.log"))
LOG_MAX_BYTES = int(env("LOG_MAX_BYTES", str(1 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(env("LOG_BACKUP_COUNT", "5"))

PING_RE = re.compile(r"time[=<]([\d.]+)")
GATEWAY_RE = re.compile(r"default via (\S+)")


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("network_pulse_agent")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Could not open log file %s (%s); logging to stdout only", LOG_FILE, exc)

    return logger


log = setup_logging()


def ping(host: str, timeout: int):
    """Return (ok, response_ms) for a single ICMP ping to host."""
    if not host:
        return False, None
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("ping %s failed to execute: %s", host, exc)
        return False, None

    if result.returncode != 0:
        return False, None

    match = PING_RE.search(result.stdout.decode(errors="replace"))
    rtt = float(match.group(1)) if match else None
    return True, rtt


def detect_gateway():
    """Auto-detect the default route's gateway IP via `ip route`."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("could not run `ip route`: %s", exc)
        return None

    match = GATEWAY_RE.search(result.stdout.decode(errors="replace"))
    return match.group(1) if match else None


def check_internet(targets, timeout):
    """Ping external targets in order, returning on the first success."""
    for host in targets:
        ok, rtt = ping(host, timeout)
        if ok:
            return True, rtt
    return False, None


def supabase_request(method: str, path: str, payload: dict) -> bool:
    url = f"{SUPABASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Prefer", "return=minimal")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        log.error("Supabase %s %s failed: HTTP %s %s", method, path, exc.code, body)
    except urllib.error.URLError as exc:
        log.error("Supabase %s %s failed: %s", method, path, exc.reason)
    return False


def send_heartbeat(gateway_ok, internet_ok, response_ms):
    payload = {
        "device_id": DEVICE_ID,
        "gateway_ok": gateway_ok,
        "internet_ok": internet_ok,
        "response_ms": int(response_ms) if response_ms is not None else None,
    }
    supabase_request("POST", "/rest/v1/heartbeats", payload)


def update_device_status(status: str):
    payload = {
        "last_heartbeat_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "current_status": status,
    }
    supabase_request("PATCH", f"/rest/v1/devices?id=eq.{DEVICE_ID}", payload)


def main():
    log.info(
        "Starting network-pulse-agent (device_id=%s client=%s interval=%ss threshold=%s)",
        DEVICE_ID,
        CLIENT_ID or "n/a",
        CHECK_INTERVAL_SECONDS,
        FAILURE_THRESHOLD,
    )

    state = "online"
    consecutive_fail = 0
    consecutive_success = 0

    while True:
        cycle_start = time.monotonic()

        gateway_host = GATEWAY_IP or detect_gateway()
        if not gateway_host:
            log.warning("no gateway configured or detected; treating gateway check as failed")
        gateway_ok, gateway_ms = ping(gateway_host, PING_TIMEOUT_SECONDS)
        internet_ok, internet_ms = check_internet(EXTERNAL_TARGETS, PING_TIMEOUT_SECONDS)
        response_ms = gateway_ms if gateway_ms is not None else internet_ms

        cycle_ok = gateway_ok and internet_ok
        if cycle_ok:
            consecutive_success += 1
            consecutive_fail = 0
        else:
            consecutive_fail += 1
            consecutive_success = 0

        new_state = state
        if state == "online" and consecutive_fail >= FAILURE_THRESHOLD:
            new_state = "offline"
        elif state == "offline" and consecutive_success >= FAILURE_THRESHOLD:
            new_state = "online"

        if new_state != state:
            log.warning("status change: %s -> %s (gateway_ok=%s internet_ok=%s)",
                        state, new_state, gateway_ok, internet_ok)
            state = new_state
        else:
            log.info(
                "check: gateway=%s(%s) internet=%s(%s) state=%s fail_streak=%s",
                gateway_ok, gateway_host, internet_ok, EXTERNAL_TARGETS, state, consecutive_fail,
            )

        send_heartbeat(gateway_ok, internet_ok, response_ms)
        update_device_status(state)

        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0.0, CHECK_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by KeyboardInterrupt")
