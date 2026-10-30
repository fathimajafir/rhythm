# Production deployment (no Docker)

**Target:** a single Linux VM. One stateful API + N stateless MCP gateways + nginx
for TLS. Plain systemd, plain venv, plain files — nothing exotic to operate at
pilot scale (300–1,000 users).

---

## 1. Architecture

```
                    ┌───────────────────────────── VM ─────────────────────────────┐
 mobile/web ──TLS──▶ nginx ──▶ rhythm-api    (systemd, 127.0.0.1:8099, stateful)
                              /var/lib/rhythm/store.json
 agent hosts ─TLS──▶ nginx ──▶ rhythm-mcp@8090 (systemd, stateless)
                              rhythm-mcp@8091 (systemd, stateless)   … scale freely
                              └──────── all proxy to the API ────────┘
```

The one rule that shapes everything: **the API is the single writer.** The MCP
gateways are stateless proxies (`RHYTHM_API_URL`); they can be started, stopped
and duplicated freely. The API's file-backed store (`SavedStore`) snapshots
atomically on every mutation and survives restarts.

What deliberately does *not* scale yet: running two API replicas against one JSON
file would race. The store interface (`rhythm/store.py`) is the seam where a real
database slots in — that is the next milestone, before fan-out.

## 2. Install

```bash
# 1. User + layout
sudo useradd --system --home /opt/rhythm --shell /usr/sbin/nologin rhythm
sudo mkdir -p /opt/rhythm /var/lib/rhythm /etc/rhythm
sudo chown -R rhythm:rhythm /opt/rhythm /var/lib/rhythm

# 2. Code + venv
sudo -u rhythm git clone <repo> /opt/rhythm
cd /opt/rhythm
sudo -u rhythm python3.11 -m venv .venv
sudo -u rhythm .venv/bin/pip install -r requirements.txt httpx

# 3. Config
sudo cp deploy/rhythm.env.example /etc/rhythm/rhythm.env
sudo chmod 600 /etc/rhythm/rhythm.env
sudoedit /etc/rhythm/rhythm.env          # set RHYTHM_INTERNAL_TOKEN (generate below)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 4. Services
sudo cp deploy/rhythm-api.service deploy/rhythm-mcp@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rhythm-api
sudo systemctl enable --now rhythm-mcp@8090 rhythm-mcp@8091   # two gateways

# 5. Verify
curl -sf http://127.0.0.1:8099/healthz
curl -sf http://127.0.0.1:8090/healthz
sudo journalctl -u rhythm-api -f
```

## 3. Security model

| Layer | Control |
|---|---|
| Network | API and gateways bind `127.0.0.1` only. nginx is the only public face, TLS 1.2/1.3 |
| Service-to-service | `RHYTHM_INTERNAL_TOKEN` bearer checked on every `/v1/*` call; MCP gateways forward it; health probes stay open |
| MCP clients | `Authorization: Bearer <subject:scopes[:agent_id]>` per call — per-token scopes, audience binding, subject binding (an agent token for Hana cannot touch Omar's data: confused-deputy defence, tested) |
| systemd hardening | `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `CapabilityBoundingSet=`, `MemoryMax` — both units |
| Data at rest | `/var/lib/rhythm` owned by `rhythm`, mode 750; snapshot JSON contains wellness data — back it up encrypted, off-box (`deploy/backup.sh` has an rclone hook) |

**Not yet (Phase 2):** per-user device authn on the API, field-level encryption of
the snapshot, mTLS between gateways and API. The internal token + loopback binding
is the honest Phase-1 control; note it in the security review.

## 4. Backups & restore

```bash
# cron — daily 02:15, 14-day retention (deploy/backup.sh)
15 2 * * * /opt/rhythm/deploy/backup.sh >> /var/log/rhythm-backup.log 2>&1
```

Restore = stop API, copy the chosen `store-*.json` to `RHYTHM_STORE_PATH`, start
API. A corrupt snapshot is quarantined as `.corrupt` automatically and the service
starts fresh rather than crash-looping (tested).

## 5. Upgrades

```bash
cd /opt/rhythm && sudo -u rhythm git pull
sudo -u rhythm .venv/bin/pip install -r requirements.txt
sudo systemctl restart rhythm-api rhythm-mcp@8090 rhythm-mcp@8091
curl -sf http://127.0.0.1:8099/healthz && curl -sf http://127.0.0.1:8090/healthz
```

Snapshot version mismatches are refused loudly (not silently migrated) — read the
release notes, then bump `_SNAPSHOT_VERSION` with a migration if one ships.

## 6. Monitoring

- **Liveness:** `GET /healthz` on 8099 and every gateway port (systemd
  `Restart=on-failure` covers crashes; add a blackbox probe for the nginx path).
- **Logs:** journald (`journalctl -u rhythm-api -u 'rhythm-mcp@*'`). JSON access
  logs are off by default; nginx logs hold the request trail.
- **Metrics to watch:** restart count (`systemctl show -p NRestarts rhythm-api`),
  snapshot size (`/var/lib/rhythm/store.json`), 401 rate on `/v1/*` (token
  misconfiguration), 409 rate (clients skipping check-in), escalation count
  (`journalctl -u rhythm-api | grep -c escalat` — clinical ops reviews monthly).
- **Trace IDs:** agent/MCP traces are OTel-shaped in-process; wiring an exporter
  is Phase 2 work (the `Trace` class is the seam).

## 7. Scaling model

| Component | Scales? | How |
|---|---|---|
| MCP gateways | ✅ freely | `systemctl enable --now rhythm-mcp@8092 …` + add the port to the nginx upstream |
| API | ❌ single instance | by design until the store becomes a database |
| nginx | — | standard |

## 8. Honest "not production" list

Said plainly, so nobody assumes otherwise:

1. **Single-writer store.** One API replica. The DB migration is prerequisite to
   any HA story.
2. **No per-user authn on the API.** Internal token + loopback + nginx for now.
3. **No TLS inside the VM** — nginx terminates; traffic inside the VM is loopback.
4. **No automatic off-box backup** — `deploy/backup.sh` has the hook; wire rclone
   or your object store.
5. **Snapshot format is versioned but not migrated.** Bumping
   `_SNAPSHOT_VERSION` requires a migration script in the same release.
