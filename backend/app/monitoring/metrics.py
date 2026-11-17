"""Process-local Prometheus metrics, updated where the work happens.

They live in prometheus_client's default registry, together with its process
and garbage-collection metrics. Label values are bounded: route templates (not
raw paths), provider names, job names and fixed outcome names.
"""

from prometheus_client import Counter, Gauge, Histogram

# --- API ------------------------------------------------------------------------------

HTTP_REQUESTS = Counter(
    "marketdata_http_requests_total",
    "API requests, by method, route template and status code.",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "marketdata_http_request_duration_seconds",
    "API request duration, by method and route template.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# --- Providers ------------------------------------------------------------------------

PROVIDER_REQUESTS = Counter(
    "marketdata_provider_requests_total",
    "HTTP requests to providers, one per attempt, by outcome.",
    ["provider", "outcome"],
)
PROVIDER_DURATION = Histogram(
    "marketdata_provider_request_duration_seconds",
    "Duration of each HTTP request to a provider.",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60),
)
PROVIDER_RETRIES = Counter(
    "marketdata_provider_retries_total", "Provider requests retried after a failure.", ["provider"]
)
PROVIDER_LIMITER_WAIT = Counter(
    "marketdata_provider_rate_limiter_wait_seconds_total",
    "Time requests spent waiting for the provider's rate limiter.",
    ["provider"],
)
PROVIDER_BUDGET_REFUSALS = Counter(
    "marketdata_provider_budget_refusals_total",
    "Requests not sent because the provider's rate budget had no capacity in time.",
    ["provider"],
)

# --- Collectors -----------------------------------------------------------------------

COLLECTOR_RUNS = Counter(
    "marketdata_collector_runs_total",
    "Finished job runs in this process, by job, kind, trigger and status.",
    ["job_name", "kind", "trigger", "status"],
)
COLLECTOR_DURATION = Histogram(
    "marketdata_collector_run_duration_seconds",
    "Duration of job runs in this process.",
    ["job_name"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800),
)
COLLECTOR_RECORDS = Counter(
    "marketdata_collector_records_written_total",
    "Records written by job runs in this process.",
    ["job_name"],
)
COLLECTOR_RUNNING = Gauge(
    "marketdata_collector_running", "Job runs in progress in this process.", ["job_name"]
)
