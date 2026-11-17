# The database server image: TimescaleDB with the latest Alpine security
# updates (Phase 11 dependency review, docs/deployment.md). The PostgreSQL and
# TimescaleDB versions are those of the base image.
FROM timescale/timescaledb:2.32.0-pg17

# timescaledb-parallel-copy is a bulk-import tool this platform does not use;
# removing it removes its vulnerable Go dependencies with it.
RUN apk upgrade --no-cache \
    && rm -f /usr/local/bin/timescaledb-parallel-copy
