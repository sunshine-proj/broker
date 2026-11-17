# Deployment

How to run the platform in production: the server, the configuration, HTTPS, access, backups and restores, upgrades, and the security measures in place. Development setup is in the [README](../README.md).

## What Production Looks Like

```text
              internet
                 |
        80, 443  |  (the only published ports)
            +----+----+
            |  proxy  |  Caddy: HTTPS (Let's Encrypt), security headers
            +----+----+
   /grafana/ ... | ... /api/v1, /docs (basic auth)
   +-------------+--------------+
   | grafana                    | backend ---- providers (internet)
   |   |                        |   |
   +---+------ internal network (no route out) ------+
       |            |           |   |               |
       db        prometheus  alertmanager ----- notifications (internet)
       |
     backup --> BACKUP_DIR (copy it off the server)
```

- **Two Compose files:** `docker-compose.yml` is the whole stack and publishes nothing. `docker-compose.prod.yml` adds the proxy. The production `.env` selects both with `COMPOSE_FILE`, so plain `docker compose` commands use them. `docker-compose.override.yml`, which publishes every port for development, is then not loaded.
- **Networks:** the `internal` network has no route to the outside world. The database and Prometheus are only on it. The backend (provider calls), the `migrate` service (command-line collection), Alertmanager (notifications) and the proxy are also on `egress`.
- **Not reachable from outside:** the database, Prometheus, Alertmanager and the backend's `/metrics`.

## Server Requirements

- A Linux server with Docker Engine 24 or later and the Compose plugin.
- 2 CPUs and 4 GB of memory are enough for the default instruments. The containers' limits add up to about 9 GB, but those are ceilings, not reservations. Measured use with the default instruments is about 1 GB in total.
- Disk: about 20 GB, for the database (a few hundred MB a year at the default volume, compressed after 30 days), Prometheus (15 days), and the backups kept on the server.
- A DNS name (`DOMAIN`) pointing at the server.
- Ports 80 and 443 open to the internet: Let's Encrypt validates on port 80, and plain HTTP is redirected to HTTPS.
- A firewall that allows only SSH, 80 and 443. For example, with ufw:

  ```bash
  sudo ufw default deny incoming
  sudo ufw allow OpenSSH
  sudo ufw allow 80,443/tcp
  sudo ufw allow 443/udp   # HTTP/3
  sudo ufw enable
  ```

  Docker writes its own firewall rules for published ports, bypassing ufw. That is safe here only because production publishes nothing but 80 and 443.

## Configuration

1. Clone the repository on the server, and create the production `.env` with random secrets:

   ```bash
   python3 scripts/generate_env.py --production \
       --domain data.example.com --email ops@example.com > .env
   chmod 600 .env
   ```

   This prints the API's basic-auth password once (to stderr). Store it in your password manager; only its bcrypt hash is written. Docker is needed, because the hash comes from the proxy image's `caddy hash-password`.

2. Edit `.env`:
   - `GRAFANA_ADMIN_USER`: anything but `admin`.
   - The provider keys you use (`TWELVEDATA_API_KEY` and others). See [providers/](providers/) for their licences, in particular Twelve Data's "internal non-display" free plan.
   - `ADMIN_API_KEY`: leave empty unless you need manual job triggers through the API.
   - `BACKUP_UID` and `BACKUP_GID`: the owner of `BACKUP_DIR` (`id -u`, `id -g`).

3. Check it:

   ```bash
   python3 scripts/check_env.py .env
   ```

   It fails on the development compose files, a missing domain, Let's Encrypt contact or API account, empty, short, reused or placeholder passwords, a plain-text API password, and a file other users can read.

### Settings

Every setting is in `.env.example` with a comment. The production-only ones:

| Variable | Meaning |
| --- | --- |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.prod.yml` |
| `DOMAIN` | Public host name; the certificate is issued for it |
| `ACME_EMAIL` | Contact for Let's Encrypt (expiry notices) |
| `API_USERNAME`, `API_PASSWORD_HASH` | The REST API's basic-auth account. The hash is bcrypt, in single quotes so Compose does not read its `$` signs. |
| `PROXY_HTTP_PORT`, `PROXY_HTTPS_PORT` | Host ports of the proxy (80, 443) |

### Secrets

- **Where they are:** passwords and keys live only in `.env`, mode 600, on the server. They reach the containers as environment variables. None is in the repository, an image or the database (PRD FR-005).
- **Database roles:** each service has its own, with the least privilege it needs.

  | Role | Used by | Can |
  | --- | --- | --- |
  | `POSTGRES_USER` (owner) | migrations, seed, backups, the command line | everything |
  | `BACKEND_DB_USER` | the running backend | read, insert, update; no delete, no truncate, no schema changes |
  | `GRAFANA_DB_USER` | Grafana | read only, read-only transactions, 30-second statements |

  The `migrate` service creates the two restricted roles and refreshes their passwords and grants on every start.
- **Rotating a password:**
  - `BACKEND_DB_PASSWORD` or `GRAFANA_DB_PASSWORD`: change it in `.env` and run `docker compose up -d`; `migrate` updates the role.
  - `POSTGRES_PASSWORD`: change it in the database first (`ALTER ROLE ... PASSWORD`), then in `.env`. The database volume keeps the password it was created with.
  - `GRAFANA_ADMIN_PASSWORD`: change it in Grafana, for the same reason.
  - The API password: create a new hash with `docker run --rm -it caddy:2.11.9-alpine caddy hash-password`, put it in `API_PASSWORD_HASH`, then `docker compose up -d proxy`.
- **Provider keys:** if one leaks, revoke it at the provider and set the new one.

## Starting

```bash
docker compose up -d --build
docker compose ps
```

All services should be running and healthy, and `migrate` should have exited with 0. Caddy obtains the certificate on the first request to `https://DOMAIN`; `docker compose logs proxy` shows it.

### Verifying the Deployment

From any machine:

```bash
API_PASSWORD='the password generate_env printed' scripts/smoke_test.sh https://data.example.com
```

It checks 15 things:
- the redirect to Grafana, Grafana's health and its login;
- the API refusing requests without credentials or with a wrong password, and answering with them;
- the API data and documentation;
- manual triggers still needing the administrator key;
- `/metrics` and unknown paths not being served;
- HSTS and the other security headers;
- the redirect from HTTP to HTTPS.

Then sign in to Grafana and check the System Monitoring dashboard: everything up, no firing alerts.

## Access

| What | Where | Who |
| --- | --- | --- |
| Grafana | `https://DOMAIN/grafana/` (`/` redirects there) | Grafana accounts |
| REST API | `https://DOMAIN/api/v1/...`, documentation at `/docs` | The basic-auth account |
| Prometheus, Alertmanager | Not published; use an SSH tunnel | Server administrators |

### Grafana

- **Accounts:** create them as the administrator (Administration, Users), with the Viewer role for people who only look at dashboards. Sign-up, anonymous access and organisation creation are off.
- **No public sharing:** public dashboards and external snapshots are disabled. The providers' licences do not allow redistributing their data, so share access only with people entitled to it.
- **Cookies:** secure (HTTPS only) and same-site strict. Grafana enforces HSTS as well. Repeated failed logins are throttled (Grafana's brute-force protection).
- **No calls home:** usage reporting, update checks and the news feed are off. Plugins cannot be installed from the UI.

### The REST API

- **Read-only:** every read endpoint needs the basic-auth account, which covers the whole API. The only write endpoint, the manual job trigger, also needs `ADMIN_API_KEY`, and is disabled while that is empty.
- **Who gets it:** anyone with the credentials can read all collected data. Give them only to people the provider licences allow to see it.
- **No rate limiting:** the proxy does not limit request rates. The API is read-only and paginated, and the account is shared, so treat the credentials like a password.
- **No CORS:** browsers on other sites cannot call the API.

### Prometheus and Alertmanager

They have no login and are not published. To look at them, publish them on the server's loopback address for a while, with the development override applied to these two services only:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.override.yml \
    up -d prometheus alertmanager
```

Then tunnel from your machine, and open <http://localhost:9090> and <http://localhost:9093>:

```bash
ssh -L 9090:127.0.0.1:9090 -L 9093:127.0.0.1:9093 server
```

Unpublish them again with `docker compose up -d prometheus alertmanager`. Day to day, their data is on the Grafana dashboards, and alerts reach you through the notifications set up in [monitoring.md](monitoring.md).

## Backups

The `backup` service backs up the database every `BACKUP_INTERVAL_HOURS` (default 24) and keeps the newest `BACKUP_KEEP` (default 7) in `BACKUP_DIR` (default `./backups`).

- **Format:** `pg_dump` custom format, without owners or privileges. The roles and grants are recreated by `migrate` after a restore.
- **Integrity:** each dump is checked with `pg_restore --list` before it counts, and has a `.sha256` checksum beside it.
- **Access:** files are readable by their owner only, because they hold every stored value.
- **Records:** each run is recorded in the `database_backup` table and shown on the System Monitoring dashboard. The alerts `BackupFailed`, `BackupMissing` and `BackupNeverRun` cover failures (see [monitoring.md](monitoring.md)).
- **Restarts:** a restart does not make an extra backup; the next one is timed from the last success. A failed backup is retried after an hour.

**Copy the backups off the server.** A backup on the same disk as the database does not survive the loss of that disk. For example, a nightly `rsync` or `rclone` of `BACKUP_DIR` to another machine or object storage, after 03:00. Encrypt the copies if they leave infrastructure you control.

Make a backup now:

```bash
docker compose run --rm --no-deps --entrypoint /bin/sh backup /backup/run-backup.sh
```

Not in the backup: Grafana's own database (users, preferences) and Prometheus's metrics. Dashboards and datasources come from the repository anyway. To keep Grafana's users, back up the `grafana_data` volume, for example by archiving it with `docker run --rm -v market-data-platform_grafana_data:/data -v "$PWD":/out alpine tar czf /out/grafana.tgz -C /data .`.

## Restoring

### Into a Separate Database

```bash
scripts/restore.sh backups/marketdata-20261117T030000Z.dump marketdata_restore
```

This creates `marketdata_restore` and restores into it, after checking the checksum. It follows TimescaleDB's procedure: the extension, then `timescaledb_pre_restore()`, then `pg_restore`, then `timescaledb_post_restore()`. Use it to look at old data, or to test a backup.

### Testing Backup and Restore

```bash
scripts/test_backup_restore.sh
```

This makes a backup, restores it into a scratch database, and checks that the restore holds everything the live database held when the backup started. Entity tables are compared row for row. Time-series rows are compared for those stored before the backup; stored rows never change, so their counts must be equal. It also checks the schema version, the hypertables and compressed chunks, then drops the scratch database. Run it after any change to the backup setup, and periodically, for example monthly.

### Restoring the Live Database

1. Stop everything that writes, and keep the database running:

   ```bash
   docker compose stop proxy backend grafana backup
   ```

2. Restore into a new database, as above, and check it:

   ```bash
   scripts/restore.sh backups/marketdata-20261117T030000Z.dump marketdata_restored
   ```

3. Swap the databases, keeping the old one until you are sure. Use the `POSTGRES_USER` from `.env`:

   ```bash
   docker compose exec db psql -U marketdata -d postgres \
       -c "ALTER DATABASE marketdata RENAME TO marketdata_old" \
       -c "ALTER DATABASE marketdata_restored RENAME TO marketdata"
   ```

   Disconnect any remaining sessions first if PostgreSQL refuses (`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'marketdata'`).

4. Start again. `migrate` re-applies migrations, the seed file, the storage policies and the database roles:

   ```bash
   docker compose up -d
   ```

5. When everything works: `docker compose exec db psql -U marketdata -d postgres -c "DROP DATABASE marketdata_old"`.

Collection resumes from the restored state. Values the providers still have for the lost period appear as gaps, which backfill re-fetches.

### Restoring onto a New Server

1. Set up the server and `.env` as above. Use the same `POSTGRES_USER`, and new passwords are fine.
2. `docker compose up -d db` and wait until it is healthy.
3. Restore with a temporary name and swap it in as in steps 2 and 3 above.
4. `docker compose up -d`.

## Upgrades

```bash
git pull
python3 scripts/check_env.py .env     # new settings appear in .env.example
docker compose build --pull
docker compose up -d
```

- `migrate` applies new migrations before the backend starts.
- Make a backup before an upgrade that includes migrations, and read `ROADMAP.md` and the migration files for anything irreversible.
- To roll back the code, check out the previous version and run `docker compose up -d --build`.
- To roll back a migration, run `docker compose run --rm migrate alembic downgrade <revision>` first. Every migration has a working downgrade, but a downgrade can drop what the newer schema stored; restoring the pre-upgrade backup is the safer way back.

Image versions are pinned in the Compose files and Dockerfiles. Update them deliberately, and rescan with Trivy as in "Dependency Review".

## Container Settings

| Setting | Where | Why |
| --- | --- | --- |
| Restart `unless-stopped` | every long-running service | Restarts after a crash or a reboot. `migrate` is `"no"`: it runs once per start. |
| Health checks | db, backend, grafana, prometheus, alertmanager, proxy | Dependent services wait for health; `docker compose ps` shows it. |
| `stop_grace_period` | db (60 s), backend (30 s) | A clean database shutdown; the current collection request finishes. |
| Memory and CPU limits | every service | One misbehaving service cannot starve the others. db 2 GB, backend 1 GB, Grafana 1 GB, Prometheus 1 GB, migrate 1 GB, backup 512 MB, proxy and Alertmanager 256 MB. |
| Log rotation | every service | `json-file`, 5 files of 10 MB each. |
| `no-new-privileges` | every service | No privilege gain through setuid binaries. |
| All capabilities dropped | every service except db | The proxy gets back only `NET_BIND_SERVICE`, for ports 80 and 443. The database's entrypoint needs its capabilities to drop root. |
| Read-only root filesystem | every service except db and Grafana | Writes go only to their volumes and a `/tmp` in memory. |

To change a limit, edit the service's `deploy.resources.limits` in `docker-compose.yml`. A container that reaches its memory limit is killed and restarted.

## Dependency Review

Reviewed on 2026-11-17:

- **Python packages:** `pip-audit` on `backend/requirements.txt` and `requirements-dev.txt` found no known vulnerabilities. All versions are pinned.
- **Images:** scanned with Trivy for fixable HIGH and CRITICAL vulnerabilities.

  | Image | Findings | Action |
  | --- | --- | --- |
  | Backend (python:3.12.15-slim-bookworm) | 4, in Debian packages (OpenSSL, apt) | Fixed: the Dockerfile installs Debian's security updates. None left. |
  | Database and backup (timescale/timescaledb:2.32.0-pg17) | 171 in Alpine packages (Kerberos, libpq, PostgreSQL client packages, libxslt); 27 in Go binaries | Fixed: `db/Dockerfile` applies Alpine's updates and removes the unused `timescaledb-parallel-copy`. Left: 26 in `gosu`, which runs once at start-up to drop root and serves nothing. |
  | Proxy | Caddy 2.11.7: 10 | Moved to 2.11.9: 1 left, in a Go dependency, with no newer Caddy release. |
  | Grafana 13.3.1 | 20, in Go libraries of bundled plugins (CloudWatch, MySQL, Loki and others) | None used here except PostgreSQL. Update Grafana when a release fixes them. |
  | Prometheus v3.15.1, Alertmanager v0.34.2 | 2 each, `golang.org/x/net` | Not fixed in the newer releases either; neither is reachable from outside. |

Repeat it before deployments and after upgrades:

```bash
docker run --rm -v "$PWD/backend":/src:ro python:3.12.15-slim-bookworm \
    sh -c 'pip install -q pip-audit && pip-audit -r /src/requirements.txt -r /src/requirements-dev.txt'
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.69.1 \
    image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed market-data-platform-backend
```

## Security Checklist

- [ ] `scripts/check_env.py .env` passes, and `.env` is mode 600.
- [ ] The firewall allows only SSH, 80 and 443.
- [ ] `scripts/smoke_test.sh` passes.
- [ ] The Grafana administrator is not called `admin`, and people use their own Viewer accounts.
- [ ] The API credentials and `ADMIN_API_KEY` (if set) are known only to those who need them.
- [ ] Backups are copied off the server, and `scripts/test_backup_restore.sh` has passed.
- [ ] Alert notifications reach someone ([monitoring.md](monitoring.md), "Notifications").
- [ ] The provider licences allow the use and the audience ([providers/](providers/)).
