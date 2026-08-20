"""PostgreSQL EXPLAIN adapter with the read-only safety model (plan_phase3 §3).

Safety gates implemented here:
- Gate 2: every EXPLAIN runs inside an explicit ``transaction(readonly=True)``
  with transaction-local statement/lock timeouts (asyncpg's pool reset does
  ``RESET ALL`` on release, so session-level settings are useless — V3-1).
- Gate 3: no write-capable API exists on this class.
- Gate 4: bounded role-safety preflight; privileged roles are rejected.
- Gate 5: TCP requires sslmode in {require, verify-ca, verify-full}.

The adapter alone adds exactly one trusted EXPLAIN prefix; the caller's SQL
is always the canonical output of the statement gate (V3-2).
"""

import asyncio
import hashlib
import logging
from urllib.parse import parse_qs, unquote, urlparse

import asyncpg

from ezsql.core.sql.explain_gate import ExplainableQuery
from ezsql.core.sql.plan import (
    ParsedPlan,
    PlanParseError,
    normalize_explain_json,
)
from ezsql.db.errors import DbAdapterError

logger = logging.getLogger("ezsql.db.postgres")

# Minimum supported server version (plan_phase3 §4).
_MIN_SERVER_MAJOR = 16

# The one trusted EXPLAIN envelope. ANALYZE/BUFFERS/WAL/TIMING are never
# present. GENERIC_PLAN is appended only for parameterized queries.
_EXPLAIN_PREFIX = "EXPLAIN (FORMAT JSON, COSTS TRUE, SUMMARY TRUE, SETTINGS TRUE"
_GENERIC_PLAN_SUFFIX = ", GENERIC_PLAN TRUE"

# URI query parameters we recognize. Unknown parameters are REJECTED rather
# than silently forwarded by asyncpg as arbitrary server settings (§6).
_ALLOWED_URI_PARAMS: frozenset[str] = frozenset({
    "sslmode", "host", "port", "user", "password", "passfile",
    "sslrootcert", "sslcert", "sslkey", "sslpassword", "sslcrl",
    "connect_timeout", "application_name", "target_session_attrs",
})

_SECURE_SSL_MODES: frozenset[str] = frozenset({"require", "verify-ca", "verify-full"})

# Role preflight caps (plan_phase3 §3 Gate 4) — fail closed when exceeded.
_MAX_ROLE_MEMBERSHIPS = 100
_MAX_ROLE_RELATIONS = 5_000

# System schemas excluded from the write-privilege preflight.
_SYSTEM_SCHEMAS: frozenset[str] = frozenset({
    "pg_catalog", "information_schema", "pg_toast",
})


class DbIdentity:
    """Non-secret DB identity (plan_phase3 §6).

    Built from normalized host(s), port(s), database, role, and SSL mode
    only. Passwords, passfiles, certificate paths, and unknown query
    parameters are excluded before hashing. The fingerprint is an internal
    key component — never logged or returned.
    """

    def __init__(self, host: str, port: int, database: str, role: str,
                 ssl_mode: str, transport: str) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.role = role
        self.ssl_mode = ssl_mode
        self.transport = transport  # "tcp" | "unix"

    @property
    def fingerprint(self) -> str:
        parts = "|".join([
            self.transport, self.host, str(self.port),
            self.database, self.role, self.ssl_mode,
        ])
        return hashlib.blake2b(parts.encode("utf-8"), digest_size=16).hexdigest()

    def describe(self) -> str:
        """Safe human description (no credentials)."""
        return f"{self.transport}://{self.host}:{self.port}/{self.database}"


def parse_db_uri(uri: str) -> tuple[DbIdentity, dict[str, str]]:
    """Parse and validate a PostgreSQL connection URI.

    Returns ``(identity, connect_kwargs)`` where ``connect_kwargs`` holds
    only recognized, safe parameters for asyncpg. Raises ``DbAdapterError``
    with category ``invalid_database_config`` on any violation:
    non-Postgres scheme, missing host/database/role, insecure TCP sslmode,
    or unknown query parameters.
    """
    parsed = urlparse(uri)

    if parsed.scheme not in ("postgres", "postgresql"):
        raise DbAdapterError(
            "invalid_database_config",
            "connection URI must use postgres:// or postgresql:// scheme",
        )

    # Host, database, and role must be explicit (§6) — no PG* env defaults.
    # Unix-socket form: postgres://role@/var/run/postgresql/mydb (empty
    # host, socket dir + database in the path).
    host = parsed.hostname
    database = ""
    if not host and parsed.path:
        # Path is /<socket_dir>/<database> — split at the last component.
        path = parsed.path.lstrip("/")
        if "/" in path:
            socket_dir, _, database = path.rpartition("/")
            host = f"/{socket_dir}"
        else:
            database = path
    elif parsed.path:
        database = unquote(parsed.path.lstrip("/"))

    if not host:
        raise DbAdapterError(
            "invalid_database_config", "connection URI must include an explicit host"
        )
    if not database:
        raise DbAdapterError(
            "invalid_database_config", "connection URI must include an explicit database"
        )
    role = unquote(parsed.username) if parsed.username else ""
    if not role:
        raise DbAdapterError(
            "invalid_database_config", "connection URI must include an explicit role"
        )

    port = parsed.port or 5432

    # Query parameters: allowlist only; unknown → reject.
    query: dict[str, list[str]] = parse_qs(parsed.query, keep_blank_values=True)
    params: dict[str, str] = {}
    for key, values in query.items():
        if key not in _ALLOWED_URI_PARAMS:
            raise DbAdapterError(
                "invalid_database_config",
                f"unsupported connection URI parameter '{key}'",
            )
        params[key] = values[-1]

    # Password from the URI userinfo (never stored in identity).
    password = unquote(parsed.password) if parsed.password else None

    ssl_mode = params.get("sslmode", "")
    is_unix = host.startswith("/")  # Unix-domain socket path

    if is_unix:
        transport = "unix"
        if not ssl_mode:
            ssl_mode = "unix-socket"
    else:
        transport = "tcp"
        if ssl_mode not in _SECURE_SSL_MODES:
            raise DbAdapterError(
                "invalid_database_config",
                "TCP connections require sslmode=require|verify-ca|verify-full",
            )

    identity = DbIdentity(
        host=host, port=port, database=database, role=role,
        ssl_mode=ssl_mode, transport=transport,
    )

    # Build asyncpg connect kwargs from recognized params only.
    connect_kwargs: dict[str, str] = {"host": host, "port": str(port),
                                      "user": role, "database": database}
    if password is not None:
        connect_kwargs["password"] = password
    if "passfile" in params:
        connect_kwargs["passfile"] = params["passfile"]
    if "application_name" in params:
        connect_kwargs["application_name"] = params["application_name"]
    if "connect_timeout" in params:
        connect_kwargs["timeout"] = params["connect_timeout"]
    if "target_session_attrs" in params:
        connect_kwargs["target_session_attrs"] = params["target_session_attrs"]
    # TLS material is passed through to asyncpg's ssl handling at connect
    # time by the adapter (not persisted in identity).
    for tls_key in ("sslrootcert", "sslcert", "sslkey", "sslpassword", "sslcrl"):
        if tls_key in params:
            connect_kwargs[tls_key] = params[tls_key]

    return identity, connect_kwargs


class PostgresAdapter:
    """Read-only PostgreSQL EXPLAIN adapter (plan_phase3 §3).

    Exposes only ``connect``, ``explain``, and ``close`` — no generic
    execute, no write API. Driver connections never escape.
    """

    def __init__(
        self,
        uri: str,
        *,
        pool_min_size: int = 1,
        pool_max_size: int = 5,
        connect_timeout: float = 10.0,
        acquire_timeout: float = 5.0,
        statement_timeout: float = 30.0,
        lock_timeout: float = 5.0,
        total_timeout: float = 45.0,
        max_plan_response_bytes: int = 2_097_152,
        max_plan_nodes: int = 500,
        max_plan_depth: int = 64,
        max_plan_condition_chars: int = 1_024,
    ) -> None:
        self._identity, self._connect_kwargs = parse_db_uri(uri)
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._connect_timeout = connect_timeout
        self._acquire_timeout = acquire_timeout
        self._statement_timeout = statement_timeout
        self._lock_timeout = lock_timeout
        self._total_timeout = total_timeout
        self._max_plan_response_bytes = max_plan_response_bytes
        self._plan_limits = {
            "max_plan_nodes": max_plan_nodes,
            "max_plan_depth": max_plan_depth,
            "max_plan_condition_chars": max_plan_condition_chars,
        }
        self._pool: asyncpg.Pool | None = None
        self._server_major: int | None = None
        self._lock = asyncio.Lock()

    @property
    def identity(self) -> DbIdentity:
        """The non-secret DB identity (fingerprint used in cache keys)."""
        return self._identity

    @property
    def server_major_version(self) -> int | None:
        """Server major version discovered at connect time (None before)."""
        return self._server_major

    async def connect(self) -> None:
        """Create the pool, verify server version, and preflight the role."""
        async with self._lock:
            if self._pool is not None:
                return
            try:
                pool = await asyncio.wait_for(
                    asyncpg.create_pool(
                        dsn=None,
                        min_size=self._pool_min_size,
                        max_size=self._pool_max_size,
                        timeout=self._connect_timeout,
                        **self._connect_kwargs,
                    ),
                    timeout=self._connect_timeout,
                )
            except TimeoutError:
                raise DbAdapterError(
                    "db_connection_failed", "connection timed out",
                    driver_exception="TimeoutError",
                ) from None
            except asyncpg.PostgresError as exc:
                raise DbAdapterError(
                    "db_connection_failed", "connection failed",
                    driver_exception=type(exc).__name__,
                ) from None
            except (OSError, ValueError) as exc:
                raise DbAdapterError(
                    "db_connection_failed", "connection failed",
                    driver_exception=type(exc).__name__,
                ) from None

            try:
                async with pool.acquire(timeout=self._acquire_timeout) as conn:
                    await self._verify_version(conn)
                    await self._preflight_role(conn)
            except DbAdapterError:
                await pool.close()
                raise
            except Exception:  # noqa: BLE001 — pool unusable
                await pool.close()
                raise DbAdapterError(
                    "db_connection_failed", "connection setup failed"
                ) from None

            self._pool = pool
            logger.info(
                "db_adapter_connected: db=%s role=%s version=%d",
                self._identity.database, self._identity.role,
                self._server_major or 0,
            )

    async def _verify_version(self, conn: asyncpg.Connection) -> None:
        """Verify PostgreSQL server major version >= 16 (plan_phase3 §7.9)."""
        version_str: str = await conn.fetchval("SHOW server_version") or ""
        try:
            major = int(version_str.split(".")[0])
        except (ValueError, IndexError):
            raise DbAdapterError(
                "database_version_unsupported",
                "could not determine server version",
            ) from None
        if major < _MIN_SERVER_MAJOR:
            raise DbAdapterError(
                "database_version_unsupported",
                f"PostgreSQL {major} is below the supported minimum ({_MIN_SERVER_MAJOR})",
            )
        self._server_major = major

    async def _preflight_role(self, conn: asyncpg.Connection) -> None:
        """Reject privileged roles (plan_phase3 §3 Gate 4).

        Fails closed with ``unsafe_database_role`` when the role is a
        superuser, BYPASSRLS, can create roles/databases/temp objects, can
        create in effective search-path schemas, or holds write privileges
        on non-system relations/sequences. Caps inspected objects; exceeding
        a cap is itself unsafe.
        """
        # Superuser / BYPASSRLS / CREATEROLE / CREATEDB — one row.
        flags = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
            "FROM pg_roles WHERE rolname = current_user"
        )
        if flags is None:
            raise DbAdapterError("unsafe_database_role", "role not found")
        if flags["rolsuper"]:
            raise DbAdapterError("unsafe_database_role", "role is superuser")
        if flags["rolbypassrls"]:
            raise DbAdapterError("unsafe_database_role", "role has BYPASSRLS")
        if flags["rolcreaterole"]:
            raise DbAdapterError("unsafe_database_role", "role can create roles")
        if flags["rolcreatedb"]:
            raise DbAdapterError("unsafe_database_role", "role can create databases")

        # TEMP privilege on the current database.
        temp_grant = await conn.fetchval(
            "SELECT has_database_privilege(current_user, current_database(), 'TEMP')"
        )
        if temp_grant:
            raise DbAdapterError(
                "unsafe_database_role", "role can create temporary objects"
            )

        # CREATE on the database or any effective search-path schema.
        create_db = await conn.fetchval(
            "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
        )
        if create_db:
            raise DbAdapterError(
                "unsafe_database_role", "role has CREATE on the database"
            )
        schema_rows = await conn.fetch(
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname = ANY(current_schemas(true)) AND nspname NOT LIKE 'pg_%'"
        )
        for row in schema_rows:
            if await conn.fetchval(
                "SELECT has_schema_privilege($1, $2, 'CREATE')",
                self._identity.role, row["nspname"],
            ):
                raise DbAdapterError(
                    "unsafe_database_role",
                    f"role has CREATE on schema {row['nspname']}",
                )

        # Role memberships (inherited privileges) — bounded.
        memberships = await conn.fetch(
            "SELECT member.rolname AS member_name "
            "FROM pg_auth_members m "
            "JOIN pg_roles member ON m.member = member.oid "
            "WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
        )
        if len(memberships) > _MAX_ROLE_MEMBERSHIPS:
            raise DbAdapterError(
                "unsafe_database_role", "role membership cap exceeded"
            )

        # Effective write privileges on non-system relations/sequences —
        # bounded, covering direct, inherited, and PUBLIC grants.
        write_rows = await conn.fetch(
            "SELECT c.relname, n.nspname FROM pg_class c "
            "JOIN pg_namespace n ON c.relnamespace = n.oid "
            "WHERE c.relkind IN ('r', 'p', 'S') "
            "AND n.nspname <> ALL($1::name[]) "
            "AND (has_table_privilege(current_user, c.oid, 'INSERT') "
            "  OR has_table_privilege(current_user, c.oid, 'UPDATE') "
            "  OR has_table_privilege(current_user, c.oid, 'DELETE') "
            "  OR has_table_privilege(current_user, c.oid, 'TRUNCATE') "
            "  OR has_table_privilege(current_user, c.oid, 'REFERENCES') "
            "  OR has_table_privilege(current_user, c.oid, 'TRIGGER')) "
            "LIMIT $2",
            list(_SYSTEM_SCHEMAS), _MAX_ROLE_RELATIONS + 1,
        )
        if len(write_rows) > _MAX_ROLE_RELATIONS:
            raise DbAdapterError(
                "unsafe_database_role", "inspected relation cap exceeded"
            )
        if write_rows:
            first = write_rows[0]
            raise DbAdapterError(
                "unsafe_database_role",
                f"role has write privileges on {first['nspname']}.{first['relname']}",
            )

    async def explain(self, query: ExplainableQuery) -> ParsedPlan:
        """Run one read-only EXPLAIN and normalize the JSON plan.

        The EXPLAIN envelope is built here from constants — the caller's
        query is the gate's canonical SQL, never the raw input string.
        """
        if self._pool is None:
            raise DbAdapterError(
                "db_connection_failed", "adapter is not connected"
            )

        envelope = _EXPLAIN_PREFIX
        if query.has_placeholders:
            envelope += _GENERIC_PLAN_SUFFIX
        envelope += ") "
        explain_sql = envelope + query.canonical_sql

        try:
            async with asyncio.timeout(self._total_timeout):
                async with self._pool.acquire(timeout=self._acquire_timeout) as conn:
                    async with conn.transaction(readonly=True):
                        # Transaction-local timeouts (RESET ALL on pool
                        # release cannot remove these — V3-1).
                        await conn.execute(
                            "SELECT set_config('statement_timeout', $1, true)",
                            f"{int(self._statement_timeout * 1000)}ms",
                        )
                        await conn.execute(
                            "SELECT set_config('lock_timeout', $1, true)",
                            f"{int(self._lock_timeout * 1000)}ms",
                        )
                        raw_plan: str | None = await conn.fetchval(explain_sql)
        except TimeoutError as exc:
            raise DbAdapterError(
                "explain_total_timeout", "operation timed out",
                driver_exception=type(exc).__name__,
            ) from None
        except asyncpg.exceptions.QueryCanceledError as exc:
            raise DbAdapterError(
                "explain_timeout", "statement or lock timeout on the server",
                driver_exception=type(exc).__name__,
            ) from None
        except asyncpg.exceptions.InvalidPasswordError as exc:
            raise DbAdapterError(
                "db_connection_failed", "authentication failed",
                driver_exception=type(exc).__name__,
            ) from None
        except asyncpg.PostgresError as exc:
            # Parameter type inference failures surface here.
            raise DbAdapterError(
                "db_internal_error", "EXPLAIN failed on the server",
                driver_exception=type(exc).__name__,
            ) from None
        except (OSError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — unknown driver failure
            raise DbAdapterError(
                "db_connection_failed", "operation failed",
                driver_exception=type(exc).__name__,
            ) from None

        if not raw_plan:
            raise DbAdapterError(
                "explain_parse_failed", "empty EXPLAIN response"
            )
        if len(raw_plan.encode("utf-8")) > self._max_plan_response_bytes:
            raise DbAdapterError(
                "plan_too_large",
                f"EXPLAIN response exceeds max_plan_response_bytes "
                f"({self._max_plan_response_bytes})",
            )

        try:
            return normalize_explain_json(raw_plan, **self._plan_limits)
        except PlanParseError as exc:
            raise DbAdapterError(
                "explain_parse_failed", str(exc)
            ) from None

    async def close(self) -> None:
        """Close the pool with a bounded graceful shutdown."""
        async with self._lock:
            pool = self._pool
            self._pool = None
        if pool is None:
            return
        try:
            async with asyncio.timeout(5.0):
                await pool.close()
        except (TimeoutError, Exception):  # noqa: BLE001 — best-effort
            pool.terminate()


__all__ = ["DbIdentity", "PostgresAdapter", "parse_db_uri"]
