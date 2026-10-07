"""PostgreSQL compatibility preflight for Alembic's revision table."""

from sqlalchemy import Connection, inspect, text

ALEMBIC_VERSION_MIN_LENGTH = 64


def ensure_postgresql_alembic_version_capacity(connection: Connection) -> None:
    """Ensure long historical revision identifiers fit before Alembic runs."""
    if connection.dialect.name != "postgresql":
        return

    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(64) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
    ))
    version_column = next(
        column for column in inspect(connection).get_columns("alembic_version")
        if column["name"] == "version_num"
    )
    current_length = getattr(version_column["type"], "length", None)
    if current_length is not None and current_length < ALEMBIC_VERSION_MIN_LENGTH:
        connection.execute(text(
            "ALTER TABLE alembic_version "
            "ALTER COLUMN version_num TYPE VARCHAR(64)"
        ))
