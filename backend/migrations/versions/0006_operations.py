"""Administrator limit metadata and bounded global audio usage."""
from alembic import op

revision = "0006_operations"
down_revision = "0005_assistant_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE POLICY limits_admin ON app.user_limits FOR SELECT TO aion_runtime USING(app.is_admin());
    ALTER TABLE app.audio_usage ADD COLUMN utc_day date NOT NULL DEFAULT
      (now() AT TIME ZONE 'UTC')::date;
    CREATE FUNCTION app.reserve_audio(p_id uuid,p_seconds int,p_global_seconds int) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,app AS $$
      BEGIN
        PERFORM pg_advisory_xact_lock(6490803);
        IF p_global_seconds<0 OR (SELECT coalesce(sum(seconds),0) FROM app.audio_usage
          WHERE utc_day=(now() AT TIME ZONE 'UTC')::date)+p_seconds>p_global_seconds
          THEN RETURN false; END IF;
        RETURN app.reserve_audio(p_id,p_seconds);
      END
    $$;
    REVOKE ALL ON FUNCTION app.reserve_audio(uuid,int) FROM aion_runtime;
    REVOKE ALL ON FUNCTION app.reserve_audio(uuid,int,int) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION app.reserve_audio(uuid,int,int) TO aion_runtime;
    """)


def downgrade() -> None:
    op.execute("""
    DROP FUNCTION app.reserve_audio(uuid,int,int);
    GRANT EXECUTE ON FUNCTION app.reserve_audio(uuid,int) TO aion_runtime;
    ALTER TABLE app.audio_usage DROP COLUMN utc_day;
    DROP POLICY limits_admin ON app.user_limits;
    """)
