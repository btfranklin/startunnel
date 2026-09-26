\set ON_ERROR_STOP on

SELECT format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', :'database_name')
\gexec
SELECT format('REVOKE TEMPORARY ON DATABASE %I FROM startunnel', :'database_name')
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO startunnel', :'database_name')
\gexec

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM startunnel;
GRANT USAGE ON SCHEMA public TO startunnel;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO startunnel;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO startunnel;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO startunnel;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO startunnel;

-- The web role cannot delete collaboration history. The supervised maintenance
-- service uses the separate database administrator connection for due deletion.
SELECT format('REVOKE DELETE ON TABLE public.%I FROM startunnel', table_name)
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
      'tunnels_tunnel',
      'tunnels_tunneladdress',
      'tunnels_cycle',
      'tunnels_message',
      'tunnels_messagemention',
      'tunnels_tunnelevent',
      'tunnels_activitycheckpoint',
      'tunnels_auditevent'
  )
\gexec
