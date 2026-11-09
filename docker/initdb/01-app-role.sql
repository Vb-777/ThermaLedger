-- Create a dedicated, NON-SUPERUSER application role.
--
-- This is load-bearing, not hygiene. Postgres exempts superusers and BYPASSRLS
-- roles from every row-level security policy, silently. If the application
-- connected as the postgres superuser, every policy in this project would be
-- inert and every isolation test would pass while enforcing nothing.
--
-- `manage.py enable_rls` refuses to run if the connected role can bypass RLS,
-- and tests/test_rls_boundary.py asserts the same thing before anything else.

CREATE ROLE thermaledger_app WITH LOGIN PASSWORD 'thermaledger' NOSUPERUSER NOBYPASSRLS NOCREATEDB;

GRANT ALL PRIVILEGES ON DATABASE thermaledger TO thermaledger_app;

\connect thermaledger

GRANT ALL ON SCHEMA public TO thermaledger_app;
ALTER SCHEMA public OWNER TO thermaledger_app;

-- Django runs the migrations as this role, so it ends up owning every table.
-- That is why enable_rls also issues FORCE ROW LEVEL SECURITY: without FORCE,
-- the owner is exempt from its own tables' policies.
