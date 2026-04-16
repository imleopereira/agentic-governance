from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


def _coerce_sync_driver(url: str) -> str:
    """Rewrite async or bare Postgres URLs to the sync psycopg3 driver.

    Alembic runs in sync mode — Alembic has no async runner. The SDK's
    runtime path uses asyncpg, so customers naturally have URLs like
    ``postgresql+asyncpg://...`` or the bare ``postgresql://...`` in
    their env. Neither works for migrations:

      * ``postgresql+asyncpg://`` — asyncpg is an async driver and
        SQLAlchemy cannot use it from a sync Engine (greenlet errors).
      * ``postgresql://`` — SQLAlchemy's default for this scheme is
        psycopg2, which we deliberately did NOT add as a dependency.

    The ``[migrations]`` optional extra installs psycopg3. Its
    SQLAlchemy URL prefix is ``postgresql+psycopg://``. We rewrite the
    user's URL at runtime so ``alembic upgrade head`` "just works" with
    whatever connection string they already export for the SDK.
    """
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = None

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = _coerce_sync_driver(config.get_main_option("sqlalchemy.url") or "")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    section = dict(config.get_section(config.config_ini_section, {}) or {})
    if "sqlalchemy.url" in section:
        section["sqlalchemy.url"] = _coerce_sync_driver(section["sqlalchemy.url"])
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
