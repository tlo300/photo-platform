#!/bin/bash
# Create application roles from environment variables.
# Runs once when the PostgreSQL container is first initialised.
#
# app_user  : low-privilege runtime role; FastAPI connects as this user.
#             Row-Level Security is enforced on all user-owned tables.
# migrator  : schema-level access; Alembic uses this role for migrations only.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DATABASE_APP_USER}') THEN
            CREATE ROLE "${DATABASE_APP_USER}" WITH LOGIN PASSWORD '${DATABASE_APP_PASSWORD}';
        END IF;

        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DATABASE_MIGRATOR_USER}') THEN
            CREATE ROLE "${DATABASE_MIGRATOR_USER}" WITH LOGIN PASSWORD '${DATABASE_MIGRATOR_PASSWORD}';
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${DATABASE_APP_USER}";
    GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO "${DATABASE_MIGRATOR_USER}";

    -- migrator: full schema access for Alembic (CREATE TABLE, ALTER TABLE, etc.)
    GRANT USAGE, CREATE ON SCHEMA public TO "${DATABASE_MIGRATOR_USER}";

    -- app_user: schema visibility only; table-level grants are added by the migration
    GRANT USAGE ON SCHEMA public TO "${DATABASE_APP_USER}";
EOSQL
