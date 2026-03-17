#!/bin/bash
set -e

# Create additional databases beyond the default POSTGRES_DB (code_storage).
# This script runs before init.sql because docker-entrypoint-initdb.d/ is
# processed in alphabetical order ("00-" < "init.sql").

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE orchestration'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'orchestration')\gexec
EOSQL

echo "Database 'orchestration' is ready."
