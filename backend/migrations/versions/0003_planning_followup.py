"""Planning, durable follow-up and leased background work."""

from alembic import op

revision = "0003_planning_followup"
down_revision = "0002_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE app.projects (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      title varchar(160) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(user_id,id)
    );
    CREATE TABLE app.tasks (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      project_id uuid, title varchar(240) NOT NULL, description text NOT NULL DEFAULT '',
      first_step varchar(240) NOT NULL, due_at timestamptz, priority int NOT NULL DEFAULT 2,
      estimated_minutes int NOT NULL DEFAULT 25 CHECK(estimated_minutes BETWEEN 1 AND 1440),
      status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','cancelled')),
      version int NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,id),
      CHECK(priority BETWEEN 1 AND 3), CHECK(version>0),
      FOREIGN KEY(user_id,project_id) REFERENCES app.projects(user_id,id)
    );
    CREATE TABLE app.activities (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      task_id uuid, title varchar(240) NOT NULL, notes text NOT NULL DEFAULT '',
      starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
      fixed boolean NOT NULL DEFAULT false, keep_on_pause boolean NOT NULL DEFAULT false,
      outcome text NOT NULL DEFAULT 'unknown'
        CHECK(outcome IN ('unknown','done','partial','cancelled')),
      result_text text NOT NULL DEFAULT '', notification_overrides jsonb NOT NULL DEFAULT '{}',
      version int NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,id),
      CHECK(ends_at>starts_at), CHECK(version>0),
      FOREIGN KEY(user_id,task_id) REFERENCES app.tasks(user_id,id)
    );
    CREATE INDEX activities_schedule ON app.activities(user_id,starts_at) WHERE outcome='unknown';
    CREATE TABLE app.notification_rules (
      user_id uuid PRIMARY KEY REFERENCES app.users ON DELETE CASCADE,
      reminders boolean NOT NULL DEFAULT true, progress boolean NOT NULL DEFAULT false,
      results boolean NOT NULL DEFAULT true, channels text[] NOT NULL DEFAULT ARRAY['web'],
      lead_minutes int NOT NULL DEFAULT 60 CHECK(lead_minutes BETWEEN 0 AND 10080),
      result_delay_minutes int NOT NULL DEFAULT 30 CHECK(result_delay_minutes BETWEEN 0 AND 10080),
      progress_minutes int NOT NULL DEFAULT 120 CHECK(progress_minutes BETWEEN 1 AND 10080),
      daily_limit int NOT NULL DEFAULT 5 CHECK(daily_limit BETWEEN 0 AND 100),
      repeat_count int NOT NULL DEFAULT 1 CHECK(repeat_count BETWEEN 0 AND 5),
      repeat_minutes int NOT NULL DEFAULT 120 CHECK(repeat_minutes BETWEEN 1 AND 10080),
      tone varchar(40) NOT NULL DEFAULT 'спокойный',
      message_format varchar(40) NOT NULL DEFAULT 'кратко',
      version int NOT NULL DEFAULT 1
    );
    CREATE TABLE app.notification_intents (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      activity_id uuid NOT NULL, activity_version int NOT NULL,
      kind text NOT NULL CHECK(kind IN ('reminder','progress','result')),
      due_at timestamptz NOT NULL, repeat_number int NOT NULL DEFAULT 0,
      state text NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','delivered','answered','cancelled')),
      created_at timestamptz NOT NULL DEFAULT now(), answered_at timestamptz,
      UNIQUE(user_id,id),
      UNIQUE(user_id,activity_id,activity_version,kind,repeat_number),
      FOREIGN KEY(user_id,activity_id) REFERENCES app.activities(user_id,id) ON DELETE CASCADE
    );
    CREATE TABLE app.activity_results (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      activity_id uuid NOT NULL, intent_id uuid, outcome text NOT NULL,
      content text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(user_id,id), CHECK(outcome IN ('done','partial','cancelled')),
      FOREIGN KEY(user_id,activity_id) REFERENCES app.activities(user_id,id),
      FOREIGN KEY(user_id,intent_id) REFERENCES app.notification_intents(user_id,id)
    );
    CREATE TABLE app.background_jobs (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      kind text NOT NULL, payload jsonb NOT NULL DEFAULT '{}', dedupe_key varchar(160) NOT NULL,
      due_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
      state text NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','queued','running','done','failed','cancelled')),
      attempts int NOT NULL DEFAULT 0, lease_token uuid, lease_until timestamptz,
      finished_at timestamptz, error_code varchar(80), UNIQUE(user_id,id),
      UNIQUE(user_id,dedupe_key)
    );
    CREATE INDEX jobs_due ON app.background_jobs(state,due_at);
    CREATE TABLE app.web_notifications (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      intent_id uuid NOT NULL, title varchar(240) NOT NULL, message text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), read_at timestamptz,
      UNIQUE(user_id,id), UNIQUE(user_id,intent_id),
      FOREIGN KEY(user_id,intent_id) REFERENCES app.notification_intents(user_id,id)
    );
    CREATE TABLE app.deliveries (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      intent_id uuid, channel text NOT NULL CHECK(channel IN ('web','push','telegram')),
      state text NOT NULL DEFAULT 'prepared'
        CHECK(state IN ('prepared','sending','sent','failed','uncertain','cancelled')),
      external_id text, error_code varchar(80), created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,id),
      UNIQUE(user_id,intent_id,channel),
      FOREIGN KEY(user_id,intent_id) REFERENCES app.notification_intents(user_id,id)
    );
    CREATE TABLE app.push_subscriptions (
      id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      endpoint text NOT NULL UNIQUE, p256dh text NOT NULL, auth text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,id)
    );
    CREATE FUNCTION app.active_account(p_user uuid) RETURNS boolean
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      SELECT EXISTS(SELECT 1 FROM app.users WHERE id=p_user AND status='active' FOR SHARE)
    $$;
    CREATE FUNCTION app.claim_jobs(p_limit int, p_now timestamptz DEFAULT now())
      RETURNS TABLE(id uuid,user_id uuid,kind text,lease_token uuid)
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      BEGIN
        UPDATE app.background_jobs j SET state='failed', error_code='attempts_exhausted',
          finished_at=p_now WHERE attempts>=5 AND state IN ('pending','queued','running')
          AND (lease_until IS NULL OR lease_until<p_now);
        RETURN QUERY WITH ready AS (
          SELECT j.id FROM app.background_jobs j JOIN app.users u ON u.id=j.user_id
          WHERE u.status='active' AND j.attempts<5 AND j.due_at<=p_now AND
            (j.state='pending' OR (j.state IN ('queued','running') AND j.lease_until<p_now))
          ORDER BY j.due_at,j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT LEAST(p_limit,100)
        ) UPDATE app.background_jobs j SET state='queued', attempts=j.attempts+1,
          lease_token=gen_random_uuid(),lease_until=p_now+interval '180 seconds'
          FROM ready WHERE j.id=ready.id RETURNING j.id,j.user_id,j.kind,j.lease_token;
      END
    $$;
    CREATE FUNCTION app.register_push(p_id uuid,p_endpoint text,p_key text,p_auth text)
      RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      DECLARE owner_id uuid := nullif(current_setting('aion.user_id',true),'')::uuid;
      BEGIN
        IF owner_id IS NULL OR NOT app.active_account(owner_id) THEN RETURN NULL; END IF;
        DELETE FROM app.push_subscriptions WHERE endpoint=p_endpoint;
        INSERT INTO app.push_subscriptions(id,user_id,endpoint,p256dh,auth)
          VALUES(p_id,owner_id,p_endpoint,p_key,p_auth);
        RETURN p_id;
      END
    $$;
    REVOKE ALL ON FUNCTION app.active_account(uuid),app.claim_jobs(int,timestamptz),
      app.register_push(uuid,text,text,text) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION app.active_account(uuid),app.claim_jobs(int,timestamptz),
      app.register_push(uuid,text,text,text) TO aion_runtime;
    """)
    for table in (
        "projects",
        "tasks",
        "activities",
        "notification_rules",
        "notification_intents",
        "activity_results",
        "background_jobs",
        "web_notifications",
        "deliveries",
        "push_subscriptions",
    ):
        op.execute(f"""
        ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY;
        CREATE POLICY owner ON app.{table} TO aion_runtime USING
          (user_id=nullif(current_setting('aion.user_id',true),'')::uuid)
          WITH CHECK(user_id=nullif(current_setting('aion.user_id',true),'')::uuid);
        """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION app.register_push(uuid,text,text,text);
    DROP FUNCTION app.claim_jobs(int,timestamptz);
    DROP FUNCTION app.active_account(uuid);
    DROP TABLE app.push_subscriptions,app.deliveries,app.web_notifications,
      app.background_jobs,app.activity_results,app.notification_intents,
      app.notification_rules,app.activities,app.tasks,app.projects;
    """)
