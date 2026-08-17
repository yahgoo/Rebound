# Rebound — Tencent Cloud VPS deployment

**Deployed:** 17 Aug 2026
**Host:** `43.156.46.66` (Tencent Cloud CVM, ap-singapore-1, Ubuntu 24.04.4 LTS, user `ubuntu`)
**Path:** `/home/ubuntu/rebound`
**Port:** `8000` (TCP)
**Public URL:** `http://43.156.46.66:8000/` (healthz: `http://43.156.46.66:8000/healthz`)
**REBOUND_MODE:** `replay`

> **Note on external reachability:** verified open as of 17 Aug 2026. The Tencent Cloud *security group* (cloud-layer firewall, not reachable via SSH) now allows inbound on port 8000; external `curl http://43.156.46.66:8000/healthz` returns HTTP 200 `{"status":"ok","mode":"replay","executor":"local","surface":"operator","chaos":"none"}` (rule details in "Firewall" below).

## Why replay mode

- The public URL is internet-facing; running `live` would expose the Atlas sandbox credentials on a public box.
- The seeded-order identity pool (A2c, `fixtures/seeded_orders.json`) is limited; uncontrolled public traffic must not burn fresh identities.
- Replay serves the full happy path from committed cassettes — sufficient for a demo URL.

## Isolation approach — why systemd user service (not Docker)

Pre-flight (`ss -tlnp; which docker; which caddy; which nginx`) showed:
- No Docker, Caddy, or nginx installed.
- The host's existing pattern is **native systemd user services** (Hermes gateway runs via `pipx` + `systemctl --user`).
- Docker was therefore rejected as inconsistent with the box; Rebound runs as its own **systemd user service** (`rebound.service`), fully separate from `hermes-gateway.service`, `hermes-upgrade-guard.service`, and `wiki-server.service`.

## Service unit

`~/.config/systemd/user/rebound.service` on the VPS (tracked in this repo as `ops/rebound.service`):

```ini
[Unit]
Description=Rebound — Autonomous flight recovery agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/rebound
ExecStart=/home/ubuntu/rebound/.venv/bin/uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
Restart=unless-stopped
RestartSec=5
Environment=PYTHONPATH=/home/ubuntu/rebound
Environment=REBOUND_MODE=replay
# Limit memory to avoid interfering with Hermes gateway (191.9M baseline)
MemoryMax=1.4G
# Notify Hermes is NOT a dependency — must not delay or block Hermes boot
# No After= or Requires= referencing hermes-* units

[Install]
WantedBy=default.target
```

- `MemoryMax=1.4G` caps Rebound so it cannot starve the Hermes gateway (baseline ~190M).
- No `After=`/`Requires=` reference any Hermes unit — Rebound boot cannot delay or block Hermes.
- `systemctl --user enable rebound.service` → survives VPS reboot.

## Persistence

SQLite at `/home/ubuntu/rebound/rebound.db` (real file path; WAL + `foreign_keys` PRAGMAs from `packages/domain/db.py`). Persists across service restarts. `.env`, `rebound.db*` are gitignored / untracked.

## Env vars

`/home/ubuntu/rebound/.env` (mode 600, created on the VPS only, never committed):

| Var | Value | Note |
|---|---|---|
| `REBOUND_MODE` | `replay` | no live Atlas credentials exposed |
| `EXECUTOR` | `local` | |
| `CHAOS_PROFILE` | `none` | |
| `SURFACE` | `operator` | |
| `DAYTONA_TARGET_SANDBOXES` | `8` | |
| `MODEL_ROUTER_DEFAULT` | `gemini` | |
| `GUARDIAN_MAX_SPEND_SGD` | `800` | |
| `ATLAS_BASE_URL` | `https://sandbox.atriptech.com` | dummy boot value; never used in replay |
| `ATLAS_CLIENT_ID` | `dummy-replay-only` | placeholder, not a live credential |
| `ATLAS_CLIENT_SECRET` | `dummy-replay-only` | placeholder, not a live credential |
| `OPERATOR_TOKEN` | random 32-byte urlsafe token | generated at deploy time |
| `PUBLIC_BASE_URL` | `http://43.156.46.66:8000` | for magic links / webhooks |

## Firewall

- OS-level: `ufw` inactive; `iptables` ACCEPT policy on all chains — no OS-level change was needed or made.
- Cloud-level (Tencent Cloud security group): **applied 17 Aug 2026** — inbound rule now active:

  ```
  Type: Custom TCP | Port: 8000 | Source: 0.0.0.0/0 | Policy: Allow | Description: Rebound — replay-mode demo
  ```

  Purpose: Rebound replay-mode demo. Scoped to Rebound's port only; default policy untouched; existing ports (22 SSH, 8888 Hermes) unaffected.

  External verification (17 Aug 2026, from a non-VPS network): `curl http://43.156.46.66:8000/healthz` → HTTP 200, body `{"status":"ok","mode":"replay","executor":"local","surface":"operator","chaos":"none"}`; `curl http://43.156.46.66:8000/` → HTTP 401 `{"detail":"operator bearer token required"}` (expected — operator console requires the bearer token). Domain/TLS remains deferred; the current URL is plain HTTP on the IP.

## Hermes gateway — before/after (undisturbed)

Captured at deploy time on 17 Aug:

| Check | BEFORE | AFTER | Identical? |
|---|---|---|---|
| `systemctl --user status hermes-gateway` | `active (running)`, PID **1211495**, up since **18 Jul** | `active (running)`, PID **1211495**, up since **18 Jul** | ✅ |
| Listening ports | 22, 53, 8888 (hermes), 12451 (node) | 22, 53, 8888 (hermes), 12451 (node), **8000 (rebound)** | ✅ one new port only |
| Cross-contamination grep (Hermes vars in Rebound `.env`/environ; Rebound vars in Hermes environ) | — | zero hits | ✅ |

Re-verified after the repo-integrity reconcile (`git pull` to `6487f1a`, `systemctl --user restart rebound.service`): same PID 1211495, same uptime, healthz 200 `{"status":"ok","mode":"replay","executor":"local","surface":"operator","chaos":"none"}`.

## Repo-integrity gap discovered during deployment (fixed 17 Aug, commit `6487f1a`)

First-time deployment exposed that `origin/main` was **not self-contained** — committed code imported modules that were never committed (they existed only as untracked files in the local working tree):

| File | Referenced by committed code since | Role |
|---|---|---|
| `packages/domain/enums.py` | Task 14 | imported by 10 committed files |
| `packages/domain/db.py` | Task 17 | imported by 9 committed files |
| `packages/agents/caretaker.py` | A3 (`0d875b7`) | invoked by `ops/demo.sh` (`-m packages.agents.caretaker` receipt/parity-dump/parity-compare) |
| `packages/agents/counterfactual.py` | A3 era | imported by `caretaker.py` |

Root cause: **files simply never `git add`ed** — *not* a `.gitignore` rule (`git check-ignore` matches nothing; no `.gitignore` change was needed). The A8/A9 "PARITY OK" evidence was produced using `caretaker.py`'s parity machinery, which a fresh clone could not have run.

Fix: commit `6487f1a` tracks the authoritative local versions byte-for-byte (verified by SHA-256 match local↔VPS after pull). Verified via two fresh clean clones: web server boots (`/healthz` 200) and `python -m packages.agents.caretaker --help` lists all six subcommands with no `ModuleNotFoundError`. Task 25/26 WIP (`packages/atlas/chaos.py`, `apps/api/routes_chaos.py`, `packages/agents/smoke_execute.py`) remains untracked by design — confirmed NOT referenced by any committed file (`git grep HEAD`).

## Follow-up (not done in this pass)

- **Domain + TLS**: plain HTTP on the raw IP for now. Once DNS is ready, add a domain (e.g. `rebound.example.com`) + Caddy/nginx auto-TLS as a separate task. Do not fabricate a domain.

## How to redeploy after a future code update

```bash
# on the VPS, as user ubuntu
cd /home/ubuntu/rebound
git pull origin main
# only if pyproject.toml changed:
.venv/bin/uv pip install -e .
systemctl --user restart rebound.service
curl -s http://127.0.0.1:8000/healthz   # expect 200, mode=replay
```

Never touch `~/.hermes/`, `hermes-gateway.service`, `hermes-upgrade-guard.service`, or `wiki-server.service`. Never commit `.env` or `rebound.db*`.
