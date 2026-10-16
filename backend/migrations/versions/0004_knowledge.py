"""Private articles, revisions, attachments and portable exports."""

from alembic import op

revision = "0004_knowledge"
down_revision = "0003_planning_followup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE app.articles (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      title varchar(240) NOT NULL,body text NOT NULL DEFAULT '',
      source_text text NOT NULL DEFAULT '',
      source_type text NOT NULL DEFAULT 'manual',source_id text,tags text[] NOT NULL DEFAULT '{}',
      project_id uuid,task_id uuid,activity_id uuid,version int NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
      deleted_at timestamptz,UNIQUE(user_id,id),
      search_document tsvector GENERATED ALWAYS AS
        (setweight(to_tsvector('russian',title),'A') ||
         setweight(to_tsvector('russian',body || ' ' || source_text),'B')) STORED,
      FOREIGN KEY(user_id,project_id) REFERENCES app.projects(user_id,id),
      FOREIGN KEY(user_id,task_id) REFERENCES app.tasks(user_id,id),
      FOREIGN KEY(user_id,activity_id) REFERENCES app.activities(user_id,id)
    );
    CREATE INDEX article_search ON app.articles USING gin(search_document) WHERE deleted_at IS NULL;
    CREATE TABLE app.article_revisions (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      article_id uuid NOT NULL,version int NOT NULL,title text NOT NULL,body text NOT NULL,
      source_text text NOT NULL,tags text[] NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(user_id,article_id,version),
      FOREIGN KEY(user_id,article_id) REFERENCES app.articles(user_id,id) ON DELETE CASCADE
    );
    CREATE TABLE app.attachments (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      article_id uuid NOT NULL,filename varchar(240) NOT NULL,media_type varchar(120) NOT NULL,
      size_bytes bigint NOT NULL CHECK(size_bytes>0),storage_key text NOT NULL UNIQUE,
      state text NOT NULL DEFAULT 'uploading' CHECK(state IN ('uploading','ready','deleted')),
      created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(user_id,id),
      FOREIGN KEY(user_id,article_id) REFERENCES app.articles(user_id,id) ON DELETE CASCADE
    );
    CREATE TABLE app.exports (
      id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES app.users ON DELETE CASCADE,
      state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','ready','failed','expired')),
      storage_key text,size_bytes bigint,created_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL DEFAULT now()+interval '1 day',UNIQUE(user_id,id)
    );
    """)
    for table in ("articles", "article_revisions", "attachments", "exports"):
        op.execute(f"""
        ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY;
        CREATE POLICY owner ON app.{table} TO aion_runtime USING
          (user_id=nullif(current_setting('aion.user_id',true),'')::uuid)
          WITH CHECK(user_id=nullif(current_setting('aion.user_id',true),'')::uuid);
        """)


def downgrade() -> None:
    op.execute("DROP TABLE app.exports,app.attachments,app.article_revisions,app.articles")
