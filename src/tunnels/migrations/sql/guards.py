"""Install PostgreSQL rules that cannot be expressed as Django constraints."""

from __future__ import annotations

from typing import Any

MESSAGE_GUARDS = r"""
ALTER TABLE public.tunnels_message
ADD CONSTRAINT message_payload_actual_size_at_most_64k
CHECK (
    (
        payload_type = 'text'
        AND text_payload IS NOT NULL
        AND json_payload IS NULL
        AND byte_count = octet_length(convert_to(text_payload, 'UTF8'))
    )
    OR (
        payload_type = 'json'
        AND text_payload IS NULL
        AND json_payload IS NOT NULL
        AND json_payload::jsonb IS NOT NULL
        AND byte_count = octet_length(convert_to(json_payload, 'UTF8'))
    )
);

ALTER TABLE public.tunnels_message
ADD CONSTRAINT message_content_digest_is_sha256
CHECK (octet_length(content_digest) = 32);

CREATE FUNCTION public.startunnel_validate_message_tree()
RETURNS trigger AS $$
DECLARE
    cycle_tunnel uuid;
    cycle_state varchar;
    parent_cycle uuid;
    parent_tunnel uuid;
    parent_sequence bigint;
    parent_depth integer;
BEGIN
    SELECT tunnel_id, state INTO cycle_tunnel, cycle_state
    FROM public.tunnels_cycle
    WHERE id = NEW.cycle_id;
    IF NOT FOUND OR NEW.tunnel_id IS DISTINCT FROM cycle_tunnel THEN
        RAISE EXCEPTION 'message tunnel does not match its cycle';
    END IF;
    IF cycle_state <> 'active' THEN
        RAISE EXCEPTION 'messages can only be posted to an active cycle';
    END IF;
    IF NEW.parent_id IS NULL THEN
        IF NEW.sequence <> 1 OR NEW.depth <> 0 THEN
            RAISE EXCEPTION 'a cycle root must have sequence 1 and depth 0';
        END IF;
    ELSE
        SELECT cycle_id, tunnel_id, sequence, depth
        INTO parent_cycle, parent_tunnel, parent_sequence, parent_depth
        FROM public.tunnels_message
        WHERE id = NEW.parent_id;
        IF NOT FOUND
           OR parent_cycle IS DISTINCT FROM NEW.cycle_id
           OR parent_tunnel IS DISTINCT FROM NEW.tunnel_id THEN
            RAISE EXCEPTION 'message parent must be in the same tunnel and cycle';
        END IF;
        IF parent_sequence >= NEW.sequence THEN
            RAISE EXCEPTION 'message parent sequence must be lower than child sequence';
        END IF;
        IF NEW.depth <> parent_depth + 1 THEN
            RAISE EXCEPTION 'message depth must be parent depth plus one';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE TRIGGER startunnel_message_tree_guard
BEFORE INSERT ON public.tunnels_message
FOR EACH ROW EXECUTE FUNCTION public.startunnel_validate_message_tree();

CREATE FUNCTION public.startunnel_keep_message_immutable()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'committed messages are immutable';
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE TRIGGER startunnel_message_immutable
BEFORE UPDATE ON public.tunnels_message
FOR EACH ROW EXECUTE FUNCTION public.startunnel_keep_message_immutable();
"""


ADDRESS_GUARDS = r"""
CREATE FUNCTION public.startunnel_validate_tunnel_address()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.tunnels_tunnel WHERE id = NEW.tunnel_id
    ) THEN
        RAISE EXCEPTION 'tunnel address must belong to a tunnel';
    END IF;
    IF TG_OP = 'UPDATE'
       AND (
           NEW.tunnel_id IS DISTINCT FROM OLD.tunnel_id
           OR NEW.address_digest IS DISTINCT FROM OLD.address_digest
           OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
       ) THEN
        RAISE EXCEPTION 'tunnel address identity is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE TRIGGER startunnel_tunnel_address_guard
BEFORE INSERT OR UPDATE ON public.tunnels_tunneladdress
FOR EACH ROW EXECUTE FUNCTION public.startunnel_validate_tunnel_address();
"""


ACTIVITY_GUARDS = r"""
CREATE FUNCTION public.startunnel_validate_event_references()
RETURNS trigger AS $$
DECLARE
    cycle_tunnel uuid;
    message_tunnel uuid;
    message_cycle uuid;
    next_position bigint;
BEGIN
    IF NEW.cycle_id IS NOT NULL THEN
        SELECT tunnel_id INTO cycle_tunnel
        FROM public.tunnels_cycle
        WHERE id = NEW.cycle_id;
        IF NOT FOUND OR cycle_tunnel IS DISTINCT FROM NEW.tunnel_id THEN
            RAISE EXCEPTION 'event cycle does not match its tunnel';
        END IF;
    END IF;
    IF NEW.message_id IS NOT NULL THEN
        SELECT tunnel_id, cycle_id INTO message_tunnel, message_cycle
        FROM public.tunnels_message
        WHERE id = NEW.message_id;
        IF NOT FOUND
           OR message_tunnel IS DISTINCT FROM NEW.tunnel_id
           OR (
               NEW.cycle_id IS NOT NULL
               AND message_cycle IS DISTINCT FROM NEW.cycle_id
           ) THEN
            RAISE EXCEPTION 'event message does not match its tunnel and cycle';
        END IF;
    END IF;
    SELECT next_event_position INTO next_position
    FROM public.tunnels_tunnel
    WHERE id = NEW.tunnel_id;
    IF NOT FOUND OR NEW.position >= next_position THEN
        RAISE EXCEPTION 'event position was not allocated by its tunnel';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE TRIGGER startunnel_event_reference_guard
BEFORE INSERT OR UPDATE OF tunnel_id, cycle_id, message_id, position
ON public.tunnels_tunnelevent
FOR EACH ROW EXECUTE FUNCTION public.startunnel_validate_event_references();

CREATE FUNCTION public.startunnel_keep_event_immutable()
RETURNS trigger AS $$
DECLARE
    referenced_cycle_state varchar;
BEGIN
    IF OLD.message_id IS NOT NULL
       AND NEW.message_id IS NULL
       AND (to_jsonb(NEW) - 'message_id') = (to_jsonb(OLD) - 'message_id') THEN
        SELECT state INTO referenced_cycle_state
        FROM public.tunnels_cycle
        WHERE id = OLD.cycle_id;
        IF referenced_cycle_state = 'deleted' THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'tunnel events are immutable';
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE TRIGGER startunnel_event_immutable
BEFORE UPDATE ON public.tunnels_tunnelevent
FOR EACH ROW EXECUTE FUNCTION public.startunnel_keep_event_immutable();

CREATE FUNCTION public.startunnel_validate_checkpoint()
RETURNS trigger AS $$
DECLARE
    next_position bigint;
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.tunnel_id IS DISTINCT FROM OLD.tunnel_id
        OR NEW.credential_id IS DISTINCT FROM OLD.credential_id
    ) THEN
        RAISE EXCEPTION 'activity checkpoint identity is immutable';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.last_event_position < OLD.last_event_position THEN
        RAISE EXCEPTION 'activity checkpoint cannot move backward';
    END IF;
    SELECT next_event_position INTO next_position
    FROM public.tunnels_tunnel
    WHERE id = NEW.tunnel_id;
    IF NOT FOUND OR NEW.last_event_position >= next_position THEN
        RAISE EXCEPTION 'activity checkpoint exceeds committed tunnel activity';
    END IF;
    IF NEW.last_event_position > 0 AND NOT EXISTS (
        SELECT 1 FROM public.tunnels_tunnelevent
        WHERE tunnel_id = NEW.tunnel_id
          AND position = NEW.last_event_position
    ) THEN
        RAISE EXCEPTION 'activity checkpoint does not name a committed event';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE TRIGGER startunnel_checkpoint_guard
BEFORE INSERT OR UPDATE OF tunnel_id, credential_id, last_event_position
ON public.tunnels_activitycheckpoint
FOR EACH ROW EXECUTE FUNCTION public.startunnel_validate_checkpoint();
"""


LIFECYCLE_GUARDS = r"""
CREATE FUNCTION public.startunnel_validate_cycle_root_id(target_cycle uuid)
RETURNS void AS $$
DECLARE
    cycle_state varchar;
    cycle_tunnel uuid;
    declared_root uuid;
    actual_root uuid;
    root_count bigint;
BEGIN
    SELECT state, tunnel_id, root_message_id
    INTO cycle_state, cycle_tunnel, declared_root
    FROM public.tunnels_cycle
    WHERE id = target_cycle;
    IF NOT FOUND OR cycle_state = 'deleted' THEN
        RETURN;
    END IF;
    SELECT count(*) INTO root_count
    FROM public.tunnels_message
    WHERE cycle_id = target_cycle AND parent_id IS NULL;
    SELECT id INTO actual_root
    FROM public.tunnels_message
    WHERE cycle_id = target_cycle AND parent_id IS NULL
    LIMIT 1;
    IF root_count <> 1 OR declared_root IS DISTINCT FROM actual_root THEN
        RAISE EXCEPTION 'a cycle must declare exactly one root message';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.tunnels_message
        WHERE id = actual_root
          AND cycle_id = target_cycle
          AND tunnel_id = cycle_tunnel
          AND sequence = 1
          AND depth = 0
    ) THEN
        RAISE EXCEPTION 'cycle root does not match its tunnel and root position';
    END IF;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE FUNCTION public.startunnel_check_cycle_root()
RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'tunnels_cycle' THEN
        PERFORM public.startunnel_validate_cycle_root_id(
            CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END
        );
    ELSE
        PERFORM public.startunnel_validate_cycle_root_id(
            CASE WHEN TG_OP = 'DELETE' THEN OLD.cycle_id ELSE NEW.cycle_id END
        );
        IF TG_OP = 'UPDATE' AND OLD.cycle_id IS DISTINCT FROM NEW.cycle_id THEN
            PERFORM public.startunnel_validate_cycle_root_id(OLD.cycle_id);
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE CONSTRAINT TRIGGER startunnel_cycle_root_from_cycle
AFTER INSERT OR UPDATE OF root_message_id, state OR DELETE
ON public.tunnels_cycle
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.startunnel_check_cycle_root();

CREATE CONSTRAINT TRIGGER startunnel_cycle_root_from_message
AFTER INSERT OR UPDATE OF cycle_id, tunnel_id, parent_id, sequence, depth OR DELETE
ON public.tunnels_message
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.startunnel_check_cycle_root();

CREATE FUNCTION public.startunnel_validate_tunnel_lifecycle_id(target_tunnel uuid)
RETURNS void AS $$
DECLARE
    tunnel_state varchar;
    current_addresses bigint;
    active_cycles bigint;
BEGIN
    SELECT state INTO tunnel_state
    FROM public.tunnels_tunnel
    WHERE id = target_tunnel;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    SELECT count(*) INTO current_addresses
    FROM public.tunnels_tunneladdress
    WHERE tunnel_id = target_tunnel AND state = 'current';
    SELECT count(*) INTO active_cycles
    FROM public.tunnels_cycle
    WHERE tunnel_id = target_tunnel AND state = 'active';
    IF tunnel_state = 'active' THEN
        IF current_addresses <> 1 OR active_cycles <> 1 THEN
            RAISE EXCEPTION 'an active tunnel needs one current address and active cycle';
        END IF;
    ELSIF tunnel_state = 'dormant' THEN
        IF current_addresses <> 1 OR active_cycles <> 0 THEN
            RAISE EXCEPTION 'a dormant tunnel needs one current address and no active cycle';
        END IF;
    ELSIF tunnel_state = 'retired' THEN
        IF current_addresses <> 0 OR active_cycles <> 0 THEN
            RAISE EXCEPTION 'a retired tunnel cannot have a current address or active cycle';
        END IF;
    END IF;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE FUNCTION public.startunnel_check_tunnel_lifecycle()
RETURNS trigger AS $$
DECLARE
    target_tunnel uuid;
BEGIN
    IF TG_TABLE_NAME = 'tunnels_tunnel' THEN
        target_tunnel := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSE
        target_tunnel := CASE WHEN TG_OP = 'DELETE' THEN OLD.tunnel_id ELSE NEW.tunnel_id END;
        IF TG_OP = 'UPDATE'
           AND OLD.tunnel_id IS DISTINCT FROM NEW.tunnel_id THEN
            PERFORM public.startunnel_validate_tunnel_lifecycle_id(OLD.tunnel_id);
        END IF;
    END IF;
    PERFORM public.startunnel_validate_tunnel_lifecycle_id(target_tunnel);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;

CREATE CONSTRAINT TRIGGER startunnel_lifecycle_from_tunnel
AFTER INSERT OR UPDATE OF state OR DELETE ON public.tunnels_tunnel
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.startunnel_check_tunnel_lifecycle();

CREATE CONSTRAINT TRIGGER startunnel_lifecycle_from_address
AFTER INSERT OR UPDATE OF tunnel_id, state OR DELETE ON public.tunnels_tunneladdress
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.startunnel_check_tunnel_lifecycle();

CREATE CONSTRAINT TRIGGER startunnel_lifecycle_from_cycle
AFTER INSERT OR UPDATE OF tunnel_id, state OR DELETE ON public.tunnels_cycle
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.startunnel_check_tunnel_lifecycle();
"""


REVOKE_HISTORY_DELETE = r"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'startunnel') THEN
        REVOKE DELETE ON TABLE
            public.tunnels_tunnel,
            public.tunnels_tunneladdress,
            public.tunnels_cycle,
            public.tunnels_message,
            public.tunnels_messagemention,
            public.tunnels_tunnelevent,
            public.tunnels_activitycheckpoint,
            public.tunnels_auditevent
        FROM startunnel;
    END IF;
END
$$;
"""


def revoke_runtime_history_delete(apps: Any, schema_editor: Any) -> None:
    del apps
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVOKE_HISTORY_DELETE)


def install_guards(apps: Any, schema_editor: Any) -> None:
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "\n".join((MESSAGE_GUARDS, ADDRESS_GUARDS, ACTIVITY_GUARDS, LIFECYCLE_GUARDS))
        )
        revoke_runtime_history_delete(apps, schema_editor)


def remove_guards(apps: Any, schema_editor: Any) -> None:
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(REVERSE_SQL)


def create_search_index(apps: Any, schema_editor: Any) -> None:
    del apps
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "CREATE INDEX message_content_search ON public.tunnels_message "
            "USING GIN (to_tsvector('simple'::regconfig, "
            "COALESCE(text_payload, '') || ' ' || COALESCE(json_payload, '')))"
        )


def drop_search_index(apps: Any, schema_editor: Any) -> None:
    del apps
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP INDEX IF EXISTS public.message_content_search")


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS startunnel_lifecycle_from_cycle ON public.tunnels_cycle;
DROP TRIGGER IF EXISTS startunnel_lifecycle_from_address ON public.tunnels_tunneladdress;
DROP TRIGGER IF EXISTS startunnel_lifecycle_from_tunnel ON public.tunnels_tunnel;
DROP FUNCTION IF EXISTS public.startunnel_check_tunnel_lifecycle();
DROP FUNCTION IF EXISTS public.startunnel_validate_tunnel_lifecycle_id(uuid);
DROP TRIGGER IF EXISTS startunnel_cycle_root_from_message ON public.tunnels_message;
DROP TRIGGER IF EXISTS startunnel_cycle_root_from_cycle ON public.tunnels_cycle;
DROP FUNCTION IF EXISTS public.startunnel_check_cycle_root();
DROP FUNCTION IF EXISTS public.startunnel_validate_cycle_root_id(uuid);
DROP TRIGGER IF EXISTS startunnel_checkpoint_guard ON public.tunnels_activitycheckpoint;
DROP FUNCTION IF EXISTS public.startunnel_validate_checkpoint();
DROP TRIGGER IF EXISTS startunnel_event_immutable ON public.tunnels_tunnelevent;
DROP FUNCTION IF EXISTS public.startunnel_keep_event_immutable();
DROP TRIGGER IF EXISTS startunnel_event_reference_guard ON public.tunnels_tunnelevent;
DROP FUNCTION IF EXISTS public.startunnel_validate_event_references();
DROP TRIGGER IF EXISTS startunnel_tunnel_address_guard ON public.tunnels_tunneladdress;
DROP FUNCTION IF EXISTS public.startunnel_validate_tunnel_address();
DROP TRIGGER IF EXISTS startunnel_message_immutable ON public.tunnels_message;
DROP FUNCTION IF EXISTS public.startunnel_keep_message_immutable();
DROP TRIGGER IF EXISTS startunnel_message_tree_guard ON public.tunnels_message;
DROP FUNCTION IF EXISTS public.startunnel_validate_message_tree();
ALTER TABLE public.tunnels_message DROP CONSTRAINT IF EXISTS message_content_digest_is_sha256;
ALTER TABLE public.tunnels_message
    DROP CONSTRAINT IF EXISTS message_payload_actual_size_at_most_64k;
"""
