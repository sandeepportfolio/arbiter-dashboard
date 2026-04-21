#!/bin/bash
# Creates additional databases alongside POSTGRES_DB for Phase 4 sandbox validation.
# Mounted at /docker-entrypoint-initdb.d/init-sandbox.sh by docker-compose.
# Only runs on first volume init - subsequent container starts are no-ops.
set -e

if [ -n "$POSTGRES_MULTIPLE_DATABASES" ]; then
    echo "Creating additional databases: $POSTGRES_MULTIPLE_DATABASES"
    for db in $(echo "$POSTGRES_MULTIPLE_DATABASES" | tr ',' ' '); do
        echo "  -> CREATE DATABASE $db"
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<EOSQL
CREATE DATABASE $db;
GRANT ALL PRIVILEGES ON DATABASE $db TO $POSTGRES_USER;
EOSQL
    done
fi
