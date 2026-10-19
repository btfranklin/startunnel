DO $$
DECLARE
    history_table text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'startunnel') THEN
        FOR history_table IN
            SELECT table_name
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
        LOOP
            EXECUTE format('REVOKE DELETE ON TABLE public.%I FROM startunnel', history_table);
        END LOOP;
    END IF;
END
$$;
