CREATE OR REPLACE FUNCTION public.startunnel_guard_cycle_history()
RETURNS trigger AS $$
BEGIN
    IF current_user <> 'startunnel' THEN
        RETURN NEW;
    END IF;
    IF ROW(NEW.id, NEW.tunnel_id, NEW.creator_id, NEW.number, NEW.created_at, NEW.expires_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.tunnel_id, OLD.creator_id, OLD.number, OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'cycle identity and expiry are immutable' USING ERRCODE = '42501';
    END IF;
    IF OLD.state <> 'active' AND ROW(
        NEW.state, NEW.closed_at, NEW.delete_after, NEW.retention_seconds,
        NEW.close_reason, NEW.final_sequence, NEW.root_message_id
    ) IS DISTINCT FROM ROW(
        OLD.state, OLD.closed_at, OLD.delete_after, OLD.retention_seconds,
        OLD.close_reason, OLD.final_sequence, OLD.root_message_id
    ) THEN
        RAISE EXCEPTION 'closed cycle history is immutable' USING ERRCODE = '42501';
    END IF;
    IF OLD.state = 'active' AND NEW.state NOT IN ('active', 'closed') THEN
        RAISE EXCEPTION 'invalid cycle state change' USING ERRCODE = '42501';
    END IF;
    IF OLD.state = 'active' AND NEW.state = 'active'
       AND ROW(NEW.retention_seconds, NEW.delete_after) IS DISTINCT FROM
           ROW(OLD.retention_seconds, OLD.delete_after) THEN
        RAISE EXCEPTION 'active cycle retention is immutable' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE OR REPLACE FUNCTION public.startunnel_guard_beacon_history()
RETURNS trigger AS $$
BEGIN
    IF current_user <> 'startunnel' THEN
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['ended_at', 'end_reason']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['ended_at', 'end_reason'])
       OR (OLD.ended_at IS NOT NULL AND NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION 'published Beacon history is immutable' USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE OR REPLACE FUNCTION public.startunnel_guard_retry_receipt()
RETURNS trigger AS $$
DECLARE
    mutable_fields text[];
BEGIN
    IF current_user <> 'startunnel' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.expires_at > statement_timestamp() THEN
            RAISE EXCEPTION 'unexpired receipts cannot be deleted' USING ERRCODE = '42501';
        END IF;
        RETURN OLD;
    END IF;
    mutable_fields := CASE WHEN TG_TABLE_NAME = 'accounts_adminoperation'
        THEN ARRAY['response', 'secret_kind', 'secret_resource_id']
        ELSE ARRAY['response', 'resource_id'] END;
    IF (to_jsonb(NEW) - mutable_fields) IS DISTINCT FROM (to_jsonb(OLD) - mutable_fields)
       OR (OLD.response <> '{}'::jsonb AND NEW IS DISTINCT FROM OLD) THEN
        RAISE EXCEPTION 'receipt identity and completed results are immutable'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

DO $$
DECLARE
    table_name text;
    guard_function text;
BEGIN
    FOR table_name, guard_function IN VALUES
        ('tunnels_cycle', 'startunnel_guard_cycle_history'),
        ('tunnels_beacon', 'startunnel_guard_beacon_history'),
        ('tunnels_idempotencyrecord', 'startunnel_guard_retry_receipt'),
        ('accounts_adminoperation', 'startunnel_guard_retry_receipt')
    LOOP
        IF to_regclass('public.' || table_name) IS NOT NULL THEN
            EXECUTE format('DROP TRIGGER IF EXISTS startunnel_runtime_history_guard ON public.%I', table_name);
            EXECUTE format(
                'CREATE TRIGGER startunnel_runtime_history_guard BEFORE UPDATE%s ON public.%I '
                'FOR EACH ROW EXECUTE FUNCTION public.%I()',
                CASE WHEN guard_function = 'startunnel_guard_retry_receipt' THEN ' OR DELETE' ELSE '' END,
                table_name, guard_function
            );
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'startunnel') THEN
        FOREACH table_name IN ARRAY ARRAY['tunnels_beacon', 'tunnels_boardstate'] LOOP
            IF to_regclass('public.' || table_name) IS NOT NULL THEN
                EXECUTE format('REVOKE DELETE ON TABLE public.%I FROM startunnel', table_name);
            END IF;
        END LOOP;
        IF to_regclass('public.tunnels_auditevent') IS NOT NULL THEN
            REVOKE UPDATE ON TABLE public.tunnels_auditevent FROM startunnel;
        END IF;
    END IF;
END
$$;
