# Monitoring and Alerting

How the platform is monitored (PRD FR-013): the metrics the backend exposes, the Prometheus and Alertmanager setup, every alert and what to do about it, and how to get notified.

## Overview

```text
backend /metrics --scrape every 30 s--> Prometheus --alert rules--> Alertmanager --> receivers
        ^                                    |                                     (none by default)
        | reads the platform state           +--> Grafana: System Monitoring,
        | from the database at each scrape        Provider Availability, Collection Failures
     PostgreSQL
```

These addresses are for development, where `docker-compose.override.yml` publishes them. In production, Prometheus and Alertmanager are not published; see [deployment.md](deployment.md#prometheus-and-alertmanager).

| Service | Address (from the host) | Configuration |
| --- | --- | --- |
| Backend metrics | <http://localhost:8000/metrics> | `backend/app/monitoring/` |
| Prometheus | <http://localhost:9090> | `prometheus/prometheus.yml`, `prometheus/alerts.yml` |
| Alertmanager | <http://localhost:9093> | `alertmanager/alertmanager.yml` |
| Grafana | <http://localhost:3000> | `grafana/provisioning/datasources/prometheus.yaml` |

The ports bind to `BIND_ADDRESS` (127.0.0.1 by default) and are set by `PROMETHEUS_PORT` and `ALERTMANAGER_PORT`. Prometheus keeps metrics for `PROMETHEUS_RETENTION` (15 days by default) in the `prometheus_data` volume. Neither Prometheus nor Alertmanager has authentication: do not expose them beyond the host.

## Metrics

`GET /metrics` on the backend, in Prometheus's text format. It has no authentication and contains no credentials. It is not in the OpenAPI schema.

### From the Backend Process

These count what this backend process did since it started. Runs started from the command line (`python -m app.cli run`) are not in them; the database-derived metrics below cover those.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `marketdata_http_requests_total` | counter | `method`, `route`, `status` | API requests. `route` is the route template, such as `/api/v1/indicators/{code}`; requests that matched no route have `route="unmatched"`. |
| `marketdata_http_request_duration_seconds` | histogram | `method`, `route` | API request duration |
| `marketdata_provider_requests_total` | counter | `provider`, `outcome` | Requests to providers, one per attempt. `outcome`: `success`, `rate_limited`, `server_error`, `client_error`, `auth_error`, `timeout`, `connection_error` |
| `marketdata_provider_request_duration_seconds` | histogram | `provider` | Duration of each provider request |
| `marketdata_provider_retries_total` | counter | `provider` | Requests retried after a failure |
| `marketdata_provider_rate_limiter_wait_seconds_total` | counter | `provider` | Time spent waiting for the rate limiter |
| `marketdata_provider_budget_refusals_total` | counter | `provider` | Requests not sent because the rate budget had no capacity in time |
| `marketdata_collector_runs_total` | counter | `job_name`, `kind`, `trigger`, `status` | Finished job runs |
| `marketdata_collector_run_duration_seconds` | histogram | `job_name` | Job run duration |
| `marketdata_collector_records_written_total` | counter | `job_name` | Records written by job runs |
| `marketdata_collector_running` | gauge | `job_name` | Runs in progress |
| `process_*`, `python_*` | | | CPU, memory, open files and garbage collection of the backend |

Job names are labelled `job_name`, because Prometheus uses `job` for the scrape target.

### From the Database, at Each Scrape

The backend reads these at each scrape, with a 10-second limit. If it cannot read the database, it reports `marketdata_database_up 0` and leaves the others out.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `marketdata_database_up` | | 1 if the database could be read |
| `marketdata_provider_health_status` | `provider` | 0 healthy, 1 degraded, 2 down. Only providers in use: enabled, with an enabled job. Absent until a provider is first called. |
| `marketdata_provider_consecutive_failures` | `provider` | Failed calls in a row |
| `marketdata_job_failed` | `job_name`, `kind`, `market_hours` | 1 if the enabled job's last finished run failed |
| `marketdata_job_last_success_timestamp_seconds` | `job_name`, `kind`, `market_hours` | When its last successful run finished |
| `marketdata_job_expected_interval_seconds` | `job_name`, `kind`, `market_hours` | Its interval, or a day for a daily job |
| `marketdata_data_age_seconds` | `provider`, `asset_type`, `observation_type` | Age of the newest value |
| `marketdata_quality_open_issues` | `kind` | Open data-quality issues |
| `marketdata_stale_series` | `provider` | Series with an open stale issue |
| `marketdata_backup_last_success_timestamp_seconds` | | When the newest successful database backup finished. Absent until one has. |
| `marketdata_backup_last_size_bytes` | | Its size |
| `marketdata_backup_last_failed` | | 1 if the newest backup run failed |
| `marketdata_backup_expected_interval_seconds` | | `BACKUP_INTERVAL_HOURS`, in seconds |

## Alerts

The rules are in `prometheus/alerts.yml`. Each alert has a `severity` label: `critical` needs action now, `warning` needs a look.

### BackendDown

- **Fires when:** Prometheus cannot scrape the backend for 2 minutes (`up{job="backend"} == 0`). Severity critical.
- **Effect:** no collection and no API. Every alert based on backend metrics goes quiet, so this one stands for them.
- **Action:** `docker compose ps backend`, then `docker compose logs backend`. The backend waits for the `migrate` service, so an invalid seed file or a failed migration also keeps it down: check `docker compose logs migrate`.

### DatabaseDown

- **Fires when:** the backend cannot read the database for 1 minute (`marketdata_database_up == 0`). Severity critical.
- **Effect:** collection runs fail and the API answers with errors. Collection, provider and freshness alerts are inhibited while it fires.
- **Action:** `docker compose ps db` and `docker compose logs db`. Check disk space for the `db_data` volume. Stored data is not affected by a restart.

### MonitoringTargetDown

- **Fires when:** Prometheus or Alertmanager has not been scraped for 5 minutes. Severity warning.
- **Action:** `docker compose ps`. If Alertmanager is down, no other alert can be delivered.

### ApiErrorRateHigh

- **Fires when:** more than 5 % of API requests answer 5xx over 5 minutes, for 10 minutes. Severity warning.
- **Action:** the System Monitoring dashboard shows which routes; the backend log has the tracebacks.

### CollectorJobFailed

- **Fires when:** an enabled job's last finished run failed, for 1 minute. Severity warning.
- **Effect:** a failure is often transient; jobs with `retry_minutes` try once more. The alert resolves when a later run succeeds.
- **Action:** the error is on the Collection Failures dashboard and in `python -m app.cli jobs`. A provider failure also shows on Provider Availability.

### CollectorJobNotSucceeding

- **Fires when:** a job that does not follow market hours has not succeeded for twice its interval plus an hour, for 5 minutes. For a 15-minute job that is 90 minutes; for a daily job, 49 hours. Severity critical.
- **Not covered:** market-hours jobs, which legitimately do not run overnight or at weekends; `CollectorJobFailed` and `DataStale` cover them. A job that has never succeeded has no last success, so only `CollectorJobFailed` fires for it.
- **Action:** as for `CollectorJobFailed`. A job that is skipped rather than failed (its lock held by a stuck run) shows as running on the Collector Monitoring dashboard.

### ProviderDown

- **Fires when:** a provider's health is `down` (three or more failed calls in a row) for 5 minutes. Severity critical.
- **Action:** the last error is on the Provider Availability dashboard. `credential rejected` means a missing or wrong API key; HTTP 451 or 403 at Binance means a regional or firewall block; timeouts or 5xx are the provider's side and usually pass. The provider recovers to `healthy` on its next successful call.

### ProviderDegraded

- **Fires when:** a provider has been `degraded` (failing now and then) for an hour. Severity warning.

### ProviderRateLimited

- **Fires when:** a provider answered HTTP 429 more than 3 times in an hour. Severity warning.
- **Action:** the rate limits in `backend/seed.toml` may no longer match the provider's plan, or something else uses the same key or IP address. Re-check the provider document in `docs/providers/`.

### ProviderBudgetExhausted

- **Fires when:** the rate limiter refused requests in the last hour because the provider's budget had no capacity in time. Severity warning.
- **Action:** usually a daily or monthly quota used up by manual collection or backfill on top of the scheduled jobs. It clears when the quota resets.

### DataStale

- **Fires when:** a provider has series with an open stale issue (PRD FR-012) for 30 minutes. Severity warning.
- **Action:** the series are listed on the Data Quality dashboard. Stale data with a healthy provider often means the provider has stopped updating a symbol; check it with `python -m app.cli search`.

### BackupFailed

- **Fires when:** the newest backup run failed. Severity warning.
- **Action:** the error is in `docker compose logs backup` and the `database_backup` table, and on the System Monitoring dashboard. Common causes are a full disk at `BACKUP_DIR`, and `BACKUP_UID` not owning it. The service retries after an hour.

### BackupMissing

- **Fires when:** the last successful backup is older than the backup interval plus six hours, for 10 minutes. Severity critical.
- **Action:** check that the backup service is running (`docker compose ps backup`) and that its runs succeed. Make one by hand as in [deployment.md](deployment.md#backups).

### BackupNeverRun

- **Fires when:** the database is up but no backup has ever succeeded, for 2 hours. Severity critical. This covers a new installation whose backup service never started.

## Notifications

Alertmanager is configured without a notification channel: alerts are listed in Alertmanager (<http://localhost:9093>), in Prometheus (Alerts tab), and on the System Monitoring and Collection Failures dashboards.

To be notified, add a receiver to `alertmanager/alertmanager.yml`, point the route at it, and restart Alertmanager (`docker compose restart alertmanager`). For example, a webhook:

```yaml
route:
  receiver: team
  group_by: [alertname, provider, job_name]

receivers:
  - name: team
    webhook_configs:
      - url: https://hooks.example.com/alerts
        send_resolved: true
```

Or email, with the password in a file rather than in the configuration:

```yaml
receivers:
  - name: team
    email_configs:
      - to: ops@example.com
        from: alerts@example.com
        smarthost: smtp.example.com:587
        auth_username: alerts@example.com
        auth_password_file: /etc/alertmanager/secrets/smtp_password
```

Mount the secret file into the `alertmanager` service and keep it out of version control. To send only critical alerts somewhere, add a child route with `matchers: [severity="critical"]`.

### Grouping and Inhibition

- Alerts are grouped by alert name, provider and job, sent 30 seconds after the first one fires, and repeated every 4 hours while they fire.
- While `DatabaseDown` fires, the collection, provider and freshness alerts it causes are inhibited.
- Tested end to end on 2026-11-17: with the database stopped, `DatabaseDown` reached Alertmanager 1 minute 46 seconds later, and resolved as soon as it was back.

## Changing Alerts

1. Edit `prometheus/alerts.yml`, and the tests in `prometheus/tests/alerts_test.yml`.
2. Check: `scripts/check_monitoring.sh`. It validates the Prometheus configuration, runs the rule tests with `promtool`, and validates the Alertmanager configuration with `amtool`, all using the images Compose runs.
3. Apply: `docker compose restart prometheus`.

Update the alert table in PRD FR-013 and this document with any change.

## Dashboards

| Dashboard | Shows |
| --- | --- |
| System Monitoring | Availability of the backend, database, Prometheus and Alertmanager; firing alerts; API traffic, errors and latency; backend CPU and memory |
| Provider Availability | Health, success rate, requests by outcome, latency, retries, rate limiting and budget refusals, per provider |
| Collection Failures | Firing collection alerts, failed runs per hour, time since each job's last success, job status, failed runs with their errors |

They mix Prometheus panels (rates and history) with database panels (the current state, and runs from the command line). Collector Monitoring and Data Quality, from earlier phases, read the database only.
