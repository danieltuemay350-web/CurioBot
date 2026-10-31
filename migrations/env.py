from logging.config import fileConfig

from alembic import context
from sqlalchemy import event, engine_from_config, pool

from app.database.models import Base
from app.config import load_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
# Resolve through the same Pydantic settings source as the application: process
# environment takes precedence over .env. Double percent signs for ConfigParser;
# never print the resolved URL (even though the app currently permits SQLite only).
database_url = load_settings().database_url
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    if connectable.dialect.name == "sqlite":
        @event.listens_for(connectable, "connect")
        def enable_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
