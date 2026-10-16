from alembic import context
from sqlalchemy import create_engine, pool

from aion.db.models import Base
from aion.settings import get_settings

target_metadata = Base.metadata
url = get_settings().migration_database_url

if context.is_offline_mode():
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, include_schemas=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_schemas=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
