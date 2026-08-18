"""Unit tests for DDL parser (plan §22.3, §13)."""

from ezsql.core.schema.ddl import parse_migrations
from ezsql.core.schema.model import SchemaModel
from ezsql.server.models import FailureEnvelope


def test_create_table() -> None:
    """CREATE TABLE produces a TableDef with columns."""
    files = [
        (
            "001_init.sql",
            "CREATE TABLE users (id INT PRIMARY KEY, email VARCHAR(255) NOT NULL)",
        )
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert "users" in result.tables
    table = result.tables["users"]
    assert "id" in table.columns
    assert "email" in table.columns
    assert table.columns["id"].data_type == "INT"
    assert table.columns["email"].nullable is False


def test_alter_add_column() -> None:
    """ALTER TABLE ADD COLUMN adds a column."""
    files = [
        ("001_init.sql", "CREATE TABLE users (id INT)"),
        ("002_add_col.sql", "ALTER TABLE users ADD COLUMN email VARCHAR(255)"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert "email" in result.tables["users"].columns


def test_alter_drop_column() -> None:
    """ALTER TABLE DROP COLUMN removes a column."""
    files = [
        ("001_init.sql", "CREATE TABLE users (id INT, age INT)"),
        ("002_drop_col.sql", "ALTER TABLE users DROP COLUMN age"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert "age" not in result.tables["users"].columns
    assert "id" in result.tables["users"].columns


def test_create_index() -> None:
    """CREATE INDEX adds an index to the table."""
    files = [
        ("001_init.sql", "CREATE TABLE users (id INT, email VARCHAR(255))"),
        ("002_add_idx.sql", "CREATE INDEX idx_email ON users (email)"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert "idx_email" in result.tables["users"].indexes
    assert result.tables["users"].indexes["idx_email"].columns == ["email"]


def test_create_unique_index() -> None:
    """CREATE UNIQUE INDEX sets unique flag."""
    files = [
        ("001_init.sql", "CREATE TABLE users (id INT, email VARCHAR(255))"),
        ("002_add_idx.sql", "CREATE UNIQUE INDEX idx_email ON users (email)"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert result.tables["users"].indexes["idx_email"].unique is True


def test_partial_index_detected() -> None:
    """Partial index (WHERE clause) is detected."""
    files = [
        ("001_init.sql", "CREATE TABLE users (id INT, email VARCHAR(255), active BOOLEAN)"),
        ("002_add_idx.sql", "CREATE INDEX idx_active ON users (email) WHERE active = true"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert result.tables["users"].indexes["idx_active"].is_partial is True


def test_drop_table() -> None:
    """DROP TABLE removes the table from schema."""
    files = [
        ("001_init.sql", "CREATE TABLE users (id INT)"),
        ("002_drop.sql", "DROP TABLE users"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert "users" not in result.tables


def test_foreign_key_column_level() -> None:
    """Column-level FK is extracted as ForeignKeyDef."""
    files = [("001_init.sql", "CREATE TABLE orders (id INT, user_id INT REFERENCES users(id))")]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert len(result.foreign_keys) == 1
    fk = result.foreign_keys[0]
    assert fk.source_table == "orders"
    assert fk.source_columns == ["user_id"]
    assert fk.target_table == "users"
    assert fk.target_columns == ["id"]


def test_foreign_key_table_level() -> None:
    """Table-level FK is extracted as ForeignKeyDef."""
    files = [("001_init.sql", """
        CREATE TABLE orders (
            id INT,
            user_id INT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert len(result.foreign_keys) == 1
    fk = result.foreign_keys[0]
    assert fk.source_table == "orders"
    assert fk.source_columns == ["user_id"]
    assert fk.target_table == "users"


def test_unsupported_ddl_produces_warning() -> None:
    """Unsupported DDL produces a ParserWarning, not a crash."""
    files = [
        (
            "001_init.sql",
            "CREATE TRIGGER my_trigger BEFORE INSERT ON users FOR EACH ROW BEGIN END",
        )
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert len(result.parser_warnings) > 0


def test_empty_migration_file() -> None:
    """Empty migration file → no error, no tables."""
    files = [("001_empty.sql", "")]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert len(result.tables) == 0


def test_multi_file_accumulation() -> None:
    """Schema accumulates across multiple migration files."""
    files = [
        ("001_init.sql", "CREATE TABLE users (id INT)"),
        ("002_orders.sql", "CREATE TABLE orders (id INT)"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert "users" in result.tables
    assert "orders" in result.tables


def test_migration_ordering_numeric() -> None:
    """Numeric-prefixed migrations are ordered correctly."""
    files = [
        ("003_third.sql", "CREATE TABLE c (id INT)"),
        ("001_first.sql", "CREATE TABLE a (id INT)"),
        ("002_second.sql", "CREATE TABLE b (id INT)"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    # source_files should be in order
    assert result.source_files == ["001_first.sql", "002_second.sql", "003_third.sql"]


def test_mixed_conventions_ambiguous() -> None:
    """Mixed migration conventions → FailureEnvelope."""
    files = [
        ("001_numeric.sql", "CREATE TABLE a (id INT)"),
        ("V1__flyway.sql", "CREATE TABLE b (id INT)"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "ambiguous_migration_conventions"


def test_duplicate_versions() -> None:
    """Duplicate migration versions → FailureEnvelope."""
    files = [
        ("001_a.sql", "CREATE TABLE a (id INT)"),
        ("001_b.sql", "CREATE TABLE b (id INT)"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, FailureEnvelope)
    assert result.kind == "duplicate_migration_version"


def test_schema_model_version() -> None:
    """SchemaModel has a schema_model_version."""
    files = [("001_init.sql", "CREATE TABLE users (id INT)")]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert result.schema_model_version != ""


def test_source_files_recorded() -> None:
    """source_files lists all migration files."""
    files = [
        ("001_init.sql", "CREATE TABLE users (id INT)"),
        ("002_add.sql", "ALTER TABLE users ADD COLUMN email VARCHAR(255)"),
    ]
    result = parse_migrations(files)
    assert isinstance(result, SchemaModel)
    assert result.source_files == ["001_init.sql", "002_add.sql"]
