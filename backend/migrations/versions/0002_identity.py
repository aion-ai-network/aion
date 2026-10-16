"""Invite-only identity, sessions and tenant policies."""

from alembic import op

revision = "0002_identity"
down_revision = "0001_service_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE app.users (
      id uuid PRIMARY KEY, email varchar(254) NOT NULL UNIQUE,
      password_hash text NOT NULL,
      role varchar(16) NOT NULL DEFAULT 'member' CHECK (role IN ('admin','member')),
      status varchar(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
      created_at timestamptz NOT NULL DEFAULT now(), CHECK (email = lower(email))
    );
    CREATE TABLE app.user_profiles (
      user_id uuid PRIMARY KEY REFERENCES app.users(id) ON DELETE CASCADE,
      display_name varchar(80) NOT NULL, timezone varchar(64) NOT NULL DEFAULT 'UTC',
      day_start time NOT NULL DEFAULT '09:00', day_end time NOT NULL DEFAULT '18:00',
      quiet_start time NOT NULL DEFAULT '22:00', quiet_end time NOT NULL DEFAULT '08:00',
      mode varchar(16) NOT NULL DEFAULT 'normal' CHECK (mode IN ('normal','light','paused')),
      notifications_enabled boolean NOT NULL DEFAULT true,
      onboarding_completed boolean NOT NULL DEFAULT false,
      version integer NOT NULL DEFAULT 1 CHECK (version > 0)
    );
    CREATE TABLE app.user_limits (
      user_id uuid PRIMARY KEY REFERENCES app.users(id) ON DELETE CASCADE,
      ai_daily_requests integer NOT NULL DEFAULT 20 CHECK (ai_daily_requests >= 0),
      audio_daily_seconds integer NOT NULL DEFAULT 600 CHECK (audio_daily_seconds >= 0),
      storage_limit_mb integer NOT NULL DEFAULT 100 CHECK (storage_limit_mb >= 0)
    );
    CREATE TABLE app.invites (
      id uuid PRIMARY KEY, email varchar(254) NOT NULL, token_hash varchar(64) NOT NULL UNIQUE,
      created_by uuid NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
      created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
      revoked_at timestamptz, used_by uuid REFERENCES app.users(id) ON DELETE SET NULL,
      used_at timestamptz, CHECK (email = lower(email))
    );
    CREATE TABLE app.sessions (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
      token_hash varchar(64) NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL, revoked_at timestamptz,
      UNIQUE (user_id, id)
    );
    CREATE INDEX sessions_owner ON app.sessions(user_id);
    CREATE TABLE app.password_resets (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
      token_hash varchar(64) NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL, used_at timestamptz
    );

    -- Authentication has no user context yet. Only narrow, token-scoped functions
    -- can cross this boundary; runtime never receives table-owner credentials.
    CREATE FUNCTION app.is_admin() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
      SET search_path = pg_catalog, app AS $$
      SELECT EXISTS (SELECT 1 FROM app.users WHERE id =
        nullif(current_setting('aion.user_id', true), '')::uuid
        AND role = 'admin' AND status = 'active')
    $$;
    CREATE FUNCTION app.auth_credentials(p_email text)
      RETURNS TABLE (id uuid, password_hash text, status text)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, app AS $$
      SELECT u.id, u.password_hash, u.status::text FROM app.users u WHERE u.email = p_email
    $$;
    CREATE FUNCTION app.auth_session(p_hash text)
      RETURNS TABLE (session_id uuid, user_id uuid, email text, role text, expires_at timestamptz)
      LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, app AS $$
      SELECT s.id, u.id, u.email::text, u.role::text, s.expires_at
        FROM app.sessions s JOIN app.users u ON u.id = s.user_id
        WHERE s.token_hash = p_hash AND s.revoked_at IS NULL AND s.expires_at > now()
          AND u.status = 'active' FOR SHARE OF u
    $$;
    CREATE FUNCTION app.auth_login(p_user uuid, p_password_hash text, p_id uuid,
      p_hash text, p_expires timestamptz) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app AS $$
      BEGIN
        PERFORM 1 FROM app.users WHERE id = p_user AND status = 'active'
          AND password_hash = p_password_hash FOR SHARE;
        IF NOT FOUND THEN RETURN false; END IF;
        INSERT INTO app.sessions(id, user_id, token_hash, expires_at)
          VALUES (p_id, p_user, p_hash, p_expires);
        RETURN true;
      END
    $$;
    CREATE FUNCTION app.auth_register(p_invite text, p_password text, p_name text,
      p_user uuid, p_session uuid, p_hash text, p_expires timestamptz) RETURNS uuid
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app AS $$
      DECLARE invitation app.invites;
      BEGIN
        SELECT * INTO invitation FROM app.invites WHERE token_hash = p_invite
          AND expires_at > now() AND revoked_at IS NULL AND used_at IS NULL FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF EXISTS (SELECT 1 FROM app.users WHERE email = invitation.email) THEN
          RETURN NULL;
        END IF;
        INSERT INTO app.users(id,email,password_hash) VALUES (p_user,invitation.email,p_password);
        INSERT INTO app.user_profiles(user_id,display_name) VALUES (p_user,p_name);
        INSERT INTO app.user_limits(user_id) VALUES (p_user);
        INSERT INTO app.sessions(id,user_id,token_hash,expires_at)
          VALUES (p_session,p_user,p_hash,p_expires);
        UPDATE app.invites SET used_at = now(), used_by = p_user WHERE id = invitation.id;
        RETURN p_user;
      END
    $$;
    CREATE FUNCTION app.auth_reset(p_hash text, p_password text) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app AS $$
      DECLARE owner_id uuid;
      BEGIN
        -- Always lock the account before its sessions, matching session lookup.
        SELECT u.id INTO owner_id FROM app.users u JOIN app.password_resets r
          ON r.user_id = u.id WHERE r.token_hash = p_hash AND r.used_at IS NULL
          AND r.expires_at > now() AND u.status = 'active' FOR UPDATE OF u;
        IF NOT FOUND THEN RETURN false; END IF;
        PERFORM 1 FROM app.password_resets WHERE token_hash = p_hash
          AND used_at IS NULL AND expires_at > now() FOR UPDATE;
        IF NOT FOUND THEN RETURN false; END IF;
        UPDATE app.users SET password_hash = p_password WHERE id = owner_id;
        UPDATE app.sessions SET revoked_at = now() WHERE user_id = owner_id
          AND revoked_at IS NULL;
        UPDATE app.password_resets SET used_at = now() WHERE user_id = owner_id
          AND used_at IS NULL;
        RETURN true;
      END
    $$;
    CREATE FUNCTION app.admin_access(p_user uuid, p_status text) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app AS $$
      BEGIN
        IF NOT app.is_admin() OR p_status NOT IN ('active','disabled') THEN
          RETURN false;
        END IF;
        -- Console recovery remains available, but a web admin cannot disable admins.
        UPDATE app.users SET status = p_status WHERE id = p_user AND role <> 'admin';
        IF NOT FOUND THEN RETURN false; END IF;
        IF p_status = 'disabled' THEN
          UPDATE app.sessions SET revoked_at = now() WHERE user_id = p_user
            AND revoked_at IS NULL;
          UPDATE app.password_resets SET used_at = now() WHERE user_id = p_user
            AND used_at IS NULL;
        END IF;
        RETURN true;
      END
    $$;

    REVOKE ALL ON app.users, app.user_profiles, app.user_limits, app.invites,
      app.sessions, app.password_resets FROM aion_runtime;
    GRANT SELECT (id,email,role,status,created_at) ON app.users TO aion_runtime;
    GRANT SELECT, UPDATE ON app.user_profiles TO aion_runtime;
    GRANT SELECT ON app.user_limits TO aion_runtime;
    GRANT SELECT, INSERT, UPDATE ON app.invites TO aion_runtime;
    GRANT SELECT ON app.sessions TO aion_runtime;
    GRANT UPDATE (revoked_at) ON app.sessions TO aion_runtime;

    ALTER TABLE app.users ENABLE ROW LEVEL SECURITY;
    CREATE POLICY users_read ON app.users FOR SELECT TO aion_runtime USING
      (id = nullif(current_setting('aion.user_id', true), '')::uuid OR app.is_admin());
    ALTER TABLE app.user_profiles ENABLE ROW LEVEL SECURITY;
    CREATE POLICY profile_owner ON app.user_profiles TO aion_runtime USING
      (user_id = nullif(current_setting('aion.user_id', true), '')::uuid)
      WITH CHECK (user_id = nullif(current_setting('aion.user_id', true), '')::uuid);
    ALTER TABLE app.user_limits ENABLE ROW LEVEL SECURITY;
    CREATE POLICY limits_owner ON app.user_limits TO aion_runtime USING
      (user_id = nullif(current_setting('aion.user_id', true), '')::uuid);
    ALTER TABLE app.sessions ENABLE ROW LEVEL SECURITY;
    CREATE POLICY session_owner ON app.sessions TO aion_runtime USING
      (user_id = nullif(current_setting('aion.user_id', true), '')::uuid);
    ALTER TABLE app.invites ENABLE ROW LEVEL SECURITY;
    CREATE POLICY invites_admin ON app.invites TO aion_runtime USING (app.is_admin())
      WITH CHECK (app.is_admin() AND created_by =
        nullif(current_setting('aion.user_id', true), '')::uuid);
    ALTER TABLE app.password_resets ENABLE ROW LEVEL SECURITY;

    REVOKE ALL ON FUNCTION app.is_admin(), app.auth_credentials(text), app.auth_session(text),
      app.auth_login(uuid,text,uuid,text,timestamptz),
      app.auth_register(text,text,text,uuid,uuid,text,timestamptz),
      app.auth_reset(text,text), app.admin_access(uuid,text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION app.is_admin(), app.auth_credentials(text), app.auth_session(text),
      app.auth_login(uuid,text,uuid,text,timestamptz),
      app.auth_register(text,text,text,uuid,uuid,text,timestamptz),
      app.auth_reset(text,text), app.admin_access(uuid,text) TO aion_runtime;
    """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION app.admin_access(uuid,text);
    DROP FUNCTION app.auth_reset(text,text);
    DROP FUNCTION app.auth_register(text,text,text,uuid,uuid,text,timestamptz);
    DROP FUNCTION app.auth_login(uuid,text,uuid,text,timestamptz);
    DROP FUNCTION app.auth_session(text);
    DROP FUNCTION app.auth_credentials(text);
    DROP POLICY invites_admin ON app.invites;
    DROP POLICY users_read ON app.users;
    DROP FUNCTION app.is_admin();
    DROP TABLE app.password_resets, app.sessions, app.invites,
      app.user_limits, app.user_profiles, app.users;
    """)
