# Network Pulse — Monitoring Agent

A single-file Python 3 script that runs on a Raspberry Pi at a client site,
checks connectivity every `CHECK_INTERVAL_SECONDS`, and reports heartbeats to
Supabase. No third-party pip dependencies — standard library only, so
deployment is just `git clone` + a `.env` file + a systemd unit.

## What it does

Each cycle it:

1. Pings the local gateway (auto-detected from the default route, or fixed
   via `GATEWAY_IP`).
2. Pings external targets (`1.1.1.1`, then `8.8.8.8`) as a secondary check.
3. Debounces state locally: a single failed ping does not flip status. It
   takes `FAILURE_THRESHOLD` consecutive bad cycles to go `online -> offline`,
   and the same number of consecutive good cycles to go back
   `offline -> online`.
4. Sends a `heartbeats` row to Supabase every cycle regardless of status
   (this is what lets a backend job tell "device offline" apart from
   "network offline" — no heartbeats at all means the Pi itself is down).
5. Updates `devices.last_heartbeat_at` and `devices.current_status` every
   cycle.

**Not included yet, by design:** writing `status_events` rows and sending
Twilio alerts. The task spec calls for confirming whether that logic lives in
this script or in a Supabase Edge Function/scheduled job before building it —
see "Open question" below. Everything needed for either approach
(debounced state, per-cycle heartbeats, `current_status` on `devices`) is
already in place.

The third state, `device_unreachable`, is intentionally never set by this
script — it's meant to be inferred on the backend from a stale
`last_heartbeat_at` (a Pi that's crashed or lost power can't report its own
unreachability).

## Files

| File | Purpose |
|---|---|
| `agent.py` | The agent itself |
| `network-pulse-agent.service` | systemd unit: auto-start on boot, auto-restart on crash |
| `.env.example` | Config template — copy to `.env` per device |

## Deploying to a new Pi (per-client checklist)

**Identical across every deployment:**
- `agent.py`
- `network-pulse-agent.service`
- Python 3 (Raspberry Pi OS Lite ships this already — no pip installs needed)

**Changes per deployment** (all in `.env`, nothing in code):
- `DEVICE_ID` — the uuid of this Pi's row in the Supabase `devices` table
  (create that row first; it should already be linked to the right
  `business_id`)
- `CLIENT_ID` — optional human-readable label, local log lines only
- `SUPABASE_URL` / `SUPABASE_KEY` — same for all sites unless you're
  multi-tenant across Supabase projects
- Optionally `GATEWAY_IP`, `EXTERNAL_TARGETS`, `CHECK_INTERVAL_SECONDS`,
  `FAILURE_THRESHOLD` if a site needs non-default tuning (e.g. a flaky
  satellite link might want a higher `FAILURE_THRESHOLD`)

### Steps

```bash
# 1. Get the code onto the Pi
sudo mkdir -p /opt/network-pulse-agent
sudo git clone <this-repo-url> /tmp/network-pulse-agent-src
sudo cp /tmp/network-pulse-agent-src/network-pulse-agent/agent.py /opt/network-pulse-agent/
sudo cp /tmp/network-pulse-agent-src/network-pulse-agent/.env.example /opt/network-pulse-agent/.env

# 2. Fill in the config for THIS site
sudo nano /opt/network-pulse-agent/.env
#   -> set DEVICE_ID, SUPABASE_URL, SUPABASE_KEY at minimum

# 3. Create the log directory
sudo mkdir -p /var/log/network-pulse-agent
sudo chown pi:pi /var/log/network-pulse-agent
sudo chown -R pi:pi /opt/network-pulse-agent

# 4. Install the systemd service
sudo cp /tmp/network-pulse-agent-src/network-pulse-agent/network-pulse-agent.service \
    /etc/systemd/system/network-pulse-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now network-pulse-agent

# 5. Confirm it's alive
sudo systemctl status network-pulse-agent
journalctl -u network-pulse-agent -f
tail -f /var/log/network-pulse-agent/agent.log
```

`Restart=always` in the unit file means the agent comes back after a crash;
`WantedBy=multi-user.target` plus `enable` means it comes back after a
reboot or power loss with no manual steps.

### Quick manual test before installing the service

```bash
cd /opt/network-pulse-agent
python3 agent.py
```

`agent.py` loads `.env` from its own directory on startup if the systemd
`EnvironmentFile` hasn't already populated the environment, so this works
standalone for on-site debugging.

## Config reference (`.env`)

See `.env.example` for the full list with defaults. The two you cannot skip:
`SUPABASE_URL`, `SUPABASE_KEY`, `DEVICE_ID`.

`SUPABASE_KEY` needs `insert` on `heartbeats` and `update` on `devices`. If
you're using RLS, either use the `service_role` key (simplest, but treat it
as a secret — it's sitting in a file on a physically accessible Pi) or write
narrow policies for an anon/authenticated key scoped to those two
operations.

## Open question — debounce + alerts split

Per the original spec, `status_events` rows and Twilio alerts should only
fire when the debounced status actually changes, and that logic can live
either in this script or in a Supabase Edge Function reading the
`heartbeats` stream. This build intentionally stops short of that piece so
we can confirm the split before building it. Everything it needs
(`current_status` transitions, one heartbeat row per cycle) is already
being produced by `agent.py`.
