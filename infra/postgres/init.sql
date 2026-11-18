-- Runs once when the Postgres volume is first created.
-- The schema itself is owned by Alembic (shared/vidnotes_common/migrations) and
-- applied by the gateway container on start (`python -m vidnotes_common.migrate`).
ALTER DATABASE vn SET timezone TO 'UTC';
