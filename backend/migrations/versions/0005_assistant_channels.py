"""Shared conversation, explicit memory, metering and Telegram binding."""

from alembic import op

revision = "0005_assistant_channels"
down_revision = "0004_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE app.conversations (
      id uuid PRIMARY KEY,user_id uuid NOT NULL UNIQUE REFERENCES app.users ON DELETE CASCADE,
      active_run_id uuid,lease_until timestamptz,UNIQUE(user_id,id)
    );
    CREATE TABLE app.messages (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      conversation_id uuid NOT NULL,sequence bigint GENERATED ALWAYS AS IDENTITY,
      role text NOT NULL CHECK(role IN ('user','assistant')),content text NOT NULL,
      channel text NOT NULL CHECK(channel IN ('web','telegram')),
      request_key varchar(160) NOT NULL,state text NOT NULL DEFAULT 'ready',
      source_type text NOT NULL DEFAULT 'text',source_key text,
      created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(user_id,id),UNIQUE(user_id,request_key),
      FOREIGN KEY(user_id,conversation_id)
        REFERENCES app.conversations(user_id,id) ON DELETE CASCADE
    );
    CREATE TABLE app.assistant_runs (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      message_id uuid NOT NULL,state text NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','running','done','failed')),
      error_code varchar(80),created_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz,
      UNIQUE(user_id,id),UNIQUE(user_id,message_id),
      FOREIGN KEY(user_id,message_id) REFERENCES app.messages(user_id,id) ON DELETE CASCADE
    );
    CREATE TABLE app.memories (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      content varchar(2000) NOT NULL,source_message_id uuid,
      source_type text NOT NULL DEFAULT 'manual',
      version int NOT NULL DEFAULT 1,created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(user_id,id),
      FOREIGN KEY(user_id,source_message_id) REFERENCES app.messages(user_id,id)
    );
    CREATE TABLE app.usage_records (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      run_id uuid NOT NULL,local_day date NOT NULL,utc_day date NOT NULL,
      reserved_usd numeric(12,6) NOT NULL CHECK(reserved_usd>=0),
      charged_usd numeric(12,6),input_tokens int,output_tokens int,
      state text NOT NULL DEFAULT 'reserved',created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(user_id,id),UNIQUE(user_id,run_id)
    );
    CREATE TABLE app.audio_usage (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      local_day date NOT NULL,seconds int NOT NULL CHECK(seconds>0),
      created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(user_id,id)
    );
    CREATE TABLE app.telegram_links (
      user_id uuid PRIMARY KEY REFERENCES app.users ON DELETE CASCADE,
      telegram_id bigint NOT NULL UNIQUE,chat_id bigint NOT NULL UNIQUE,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE app.telegram_link_tokens (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      session_id uuid NOT NULL,token_hash char(64) NOT NULL UNIQUE,
      candidate_id bigint,candidate_chat bigint,expires_at timestamptz NOT NULL,
      used_at timestamptz,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(user_id,id),
      FOREIGN KEY(user_id,session_id) REFERENCES app.sessions(user_id,id) ON DELETE CASCADE
    );
    CREATE TABLE app.telegram_updates (
      update_id bigint PRIMARY KEY,created_at timestamptz NOT NULL DEFAULT now()
    );
    REVOKE ALL ON app.telegram_updates FROM aion_runtime;
    CREATE FUNCTION app.reserve_ai(p_id uuid,p_run uuid,p_cost numeric,p_cap numeric)
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      DECLARE u uuid := nullif(current_setting('aion.user_id',true),'')::uuid;
        day_local date;lim int;
      BEGIN
        IF u IS NULL OR p_cost<0 OR p_cap<0 OR NOT app.active_account(u) THEN RETURN false; END IF;
        PERFORM pg_advisory_xact_lock(6490802);
        SELECT (now() AT TIME ZONE p.timezone)::date,l.ai_daily_requests INTO day_local,lim
          FROM app.user_profiles p JOIN app.user_limits l ON l.user_id=p.user_id WHERE p.user_id=u;
        IF EXISTS(SELECT 1 FROM app.usage_records WHERE user_id=u AND run_id=p_run) THEN
          RETURN false;
        END IF;
        IF (SELECT count(*) FROM app.usage_records WHERE user_id=u AND local_day=day_local)>=lim
          OR (SELECT coalesce(sum(coalesce(charged_usd,reserved_usd)),0)
            FROM app.usage_records WHERE utc_day=(now() AT TIME ZONE 'UTC')::date)+p_cost>p_cap
          THEN RETURN false; END IF;
        INSERT INTO app.usage_records(id,user_id,run_id,local_day,utc_day,reserved_usd)
          VALUES(p_id,u,p_run,day_local,(now() AT TIME ZONE 'UTC')::date,p_cost);
        RETURN true;
      END
    $$;
    CREATE FUNCTION app.reserve_audio(p_id uuid,p_seconds int) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      DECLARE u uuid := nullif(current_setting('aion.user_id',true),'')::uuid;
        day_local date;lim int;
      BEGIN
        IF u IS NULL OR p_seconds NOT BETWEEN 1 AND 600 OR NOT app.active_account(u)
          THEN RETURN false; END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('audio:' || u::text,0));
        SELECT (now() AT TIME ZONE p.timezone)::date,l.audio_daily_seconds INTO day_local,lim
          FROM app.user_profiles p JOIN app.user_limits l ON l.user_id=p.user_id WHERE p.user_id=u;
        IF EXISTS(SELECT 1 FROM app.audio_usage WHERE id=p_id) THEN RETURN false; END IF;
        IF (SELECT coalesce(sum(seconds),0) FROM app.audio_usage
          WHERE user_id=u AND local_day=day_local)+p_seconds>lim THEN RETURN false; END IF;
        INSERT INTO app.audio_usage(id,user_id,local_day,seconds)
          VALUES(p_id,u,day_local,p_seconds);
        RETURN true;
      END
    $$;
    CREATE FUNCTION app.telegram_offer(p_hash text,p_telegram bigint,p_chat bigint) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      BEGIN
        UPDATE app.telegram_link_tokens t SET candidate_id=p_telegram,candidate_chat=p_chat
          FROM app.sessions s,app.users u WHERE t.token_hash=p_hash AND t.used_at IS NULL
          AND t.expires_at>now() AND t.candidate_id IS NULL AND s.id=t.session_id
          AND s.user_id=t.user_id AND s.revoked_at IS NULL AND s.expires_at>now()
          AND u.id=t.user_id AND u.status='active';
        RETURN FOUND;
      END
    $$;
    CREATE FUNCTION app.telegram_owner(p_telegram bigint,p_chat bigint) RETURNS uuid
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      SELECT l.user_id FROM app.telegram_links l JOIN app.users u ON u.id=l.user_id
        WHERE l.telegram_id=p_telegram AND l.chat_id=p_chat AND u.status='active'
    $$;
    CREATE FUNCTION app.telegram_dedupe(p_update bigint) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      BEGIN
        INSERT INTO app.telegram_updates(update_id) VALUES(p_update) ON CONFLICT DO NOTHING;
        RETURN FOUND;
      END
    $$;
    CREATE FUNCTION app.admin_limits(p_user uuid,p_ai int,p_audio int,p_storage int)
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      BEGIN
        IF NOT app.is_admin() OR p_ai NOT BETWEEN 0 AND 1000 OR p_audio NOT BETWEEN 0 AND 36000
          OR p_storage NOT BETWEEN 0 AND 100000 THEN RETURN false; END IF;
        UPDATE app.user_limits SET ai_daily_requests=p_ai,audio_daily_seconds=p_audio,
          storage_limit_mb=p_storage WHERE user_id=p_user;
        RETURN FOUND;
      END
    $$;
    CREATE FUNCTION app.admin_operations() RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      BEGIN
        IF NOT app.is_admin() THEN RETURN NULL; END IF;
        RETURN jsonb_build_object('jobs',(SELECT coalesce(jsonb_agg(j),'[]') FROM
          (SELECT kind,state,count(*) AS count FROM app.background_jobs GROUP BY kind,state) j),
          'usage',(SELECT jsonb_build_object('requests',count(*),
            'cost_usd',coalesce(sum(coalesce(charged_usd,reserved_usd)),0))
            FROM app.usage_records WHERE utc_day=(now() AT TIME ZONE 'UTC')::date),
          'errors',(SELECT coalesce(jsonb_agg(e),'[]') FROM
            (SELECT kind,error_code,count(*) AS count FROM app.background_jobs
              WHERE error_code IS NOT NULL GROUP BY kind,error_code) e));
      END
    $$;
    REVOKE ALL ON FUNCTION app.reserve_ai(uuid,uuid,numeric,numeric),
      app.reserve_audio(uuid,int),app.telegram_offer(text,bigint,bigint),
      app.telegram_owner(bigint,bigint),app.telegram_dedupe(bigint),
      app.admin_limits(uuid,int,int,int),app.admin_operations() FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION app.reserve_ai(uuid,uuid,numeric,numeric),
      app.reserve_audio(uuid,int),app.telegram_offer(text,bigint,bigint),
      app.telegram_owner(bigint,bigint),app.telegram_dedupe(bigint),
      app.admin_limits(uuid,int,int,int),app.admin_operations() TO aion_runtime;
    """)
    for table in (
        "conversations",
        "messages",
        "assistant_runs",
        "memories",
        "usage_records",
        "audio_usage",
        "telegram_links",
        "telegram_link_tokens",
    ):
        op.execute(f"""
        ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY;
        CREATE POLICY owner ON app.{table} TO aion_runtime USING
          (user_id=nullif(current_setting('aion.user_id',true),'')::uuid)
          WITH CHECK(user_id=nullif(current_setting('aion.user_id',true),'')::uuid);
        """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION app.reserve_ai(uuid,uuid,numeric,numeric),app.reserve_audio(uuid,int),
      app.telegram_offer(text,bigint,bigint),app.telegram_owner(bigint,bigint),
      app.telegram_dedupe(bigint),app.admin_limits(uuid,int,int,int),app.admin_operations();
    DROP TABLE app.telegram_updates,app.telegram_link_tokens,app.telegram_links,app.audio_usage,
      app.usage_records,app.memories,app.assistant_runs,app.messages,app.conversations;
    """)
