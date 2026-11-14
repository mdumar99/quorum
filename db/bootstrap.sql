-- Role bootstrap (ADR-0002, Decision 3). Idempotent: safe to run on every start.
--
-- Run as the bootstrap superuser, over TCP (the official postgres image trusts the local
-- socket and loopback, so a password is only checked on a connection from another host):
--   psql -X -v ON_ERROR_STOP=1 -v dbname=<database> -f db/bootstrap.sql
--
-- * Passwords come from the environment (QUORUM_OWNER_PASSWORD, QUORUM_APP_PASSWORD), never
--   from the command line, so they do not show up in a process list.
-- * Roles are cluster-wide, but schema grants and default privileges are per database, so run
--   this once for every database. `dbname` names it; without it the connected database is used.
-- * A role that already exists keeps its password. Changing one is a separate, documented
--   procedure (docs/database-roles.md).

-- Stop at the first error. The checks below rely on it: psql then exits non-zero.
\set ON_ERROR_STOP on

\if :{?dbname}
  \connect :"dbname"
\endif

-- Keep the clear-text passwords out of the server log: a failing statement is logged by default.
-- Per session, so this comes after \connect (which opens a new session). No server config change.
SET log_statement = 'none';
SET log_min_error_statement = 'panic';

\getenv owner_pw QUORUM_OWNER_PASSWORD
\getenv app_pw QUORUM_APP_PASSWORD
-- \getenv leaves the variable undefined when the environment variable is unset; treat that as empty.
\if :{?owner_pw}
\else
  \set owner_pw ''
\endif
\if :{?app_pw}
\else
  \set app_pw ''
\endif

-- A missing or empty password is an error (non-zero exit), never a silent success.
SELECT :'owner_pw' = '' AS owner_missing \gset
SELECT :'app_pw' = '' AS app_missing \gset
\if :owner_missing
  DO $$ BEGIN RAISE EXCEPTION 'QUORUM_OWNER_PASSWORD is not set or empty'; END $$;
\endif
\if :app_missing
  DO $$ BEGIN RAISE EXCEPTION 'QUORUM_APP_PASSWORD is not set or empty'; END $$;
\endif

SELECT format('CREATE ROLE quorum_owner LOGIN NOSUPERUSER PASSWORD %L', :'owner_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quorum_owner') \gexec
SELECT format('CREATE ROLE quorum_app LOGIN NOSUPERUSER PASSWORD %L', :'app_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quorum_app') \gexec

-- The owner runs the migrations. USAGE on schema public comes from PUBLIC; CREATE does not.
GRANT USAGE, CREATE ON SCHEMA public TO quorum_owner;

-- Tables the owner creates later: the app role gets SELECT and INSERT and nothing else.
-- UPDATE is a column-level opt-in in a later migration; DELETE and TRUNCATE are never granted.
ALTER DEFAULT PRIVILEGES FOR ROLE quorum_owner IN SCHEMA public
  GRANT SELECT, INSERT ON TABLES TO quorum_app;

-- The schema of Alembic's version table (ADR-0002, Decision 1). The owner owns it; PUBLIC and
-- the app role get nothing, so quorum_app cannot even see it. The default privileges above are
-- scoped IN SCHEMA public, which is what keeps this schema out of the app role's reach.
-- The ALTER and REVOKEs make a schema that already exists converge to the same state.
CREATE SCHEMA IF NOT EXISTS migrations AUTHORIZATION quorum_owner;
ALTER SCHEMA migrations OWNER TO quorum_owner;
REVOKE ALL ON SCHEMA migrations FROM PUBLIC;
REVOKE ALL ON SCHEMA migrations FROM quorum_app;
