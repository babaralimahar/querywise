"""SQL guard: parse to an AST (sqlglot, Postgres dialect) and allow only one bounded, read-only
SELECT over the curated views. Every block carries a reason code the UI and audit log show.

Defence in depth: even if this guard had a bug, the query still runs as analyst_ro (SELECT on
curated views only, read-only transactions, 5 s statement_timeout). The EXPLAIN cost ceiling is
checked at execution time (executor.py), because it needs the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from querywise.semantic import SemanticLayer


class Reason(StrEnum):
    EMPTY = "EMPTY"
    TOO_LONG = "TOO_LONG"
    PARSE_ERROR = "PARSE_ERROR"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    NOT_SELECT = "NOT_SELECT"
    WRITE_OPERATION = "WRITE_OPERATION"
    DISALLOWED_TABLE = "DISALLOWED_TABLE"
    DISALLOWED_COLUMN = "DISALLOWED_COLUMN"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    STAR_ON_SENSITIVE = "STAR_ON_SENSITIVE"
    DISALLOWED_FUNCTION = "DISALLOWED_FUNCTION"
    CARTESIAN_JOIN = "CARTESIAN_JOIN"
    RECURSIVE_CTE = "RECURSIVE_CTE"
    LOCKING = "LOCKING"
    BAD_LIMIT = "BAD_LIMIT"
    COST_TOO_HIGH = "COST_TOO_HIGH"  # raised by the executor's EXPLAIN check


# Mistakes the model can fix by itself (fed back to the repair loop). Everything else is a
# security block and ends the request.
REPAIRABLE = {Reason.PARSE_ERROR, Reason.UNKNOWN_COLUMN}

WRITE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Copy,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Command,
    exp.Pragma,
    exp.Use,
)

DENY_FUNCTIONS = {
    # sleeping / resource exhaustion
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "generate_series",
    "repeat",
    "lpad",
    "rpad",
    # file system and large objects
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_get",
    "lo_put",
    "lo_from_bytea",
    # server state, other sessions, settings, sequences
    "set_config",
    "current_setting",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "nextval",
    "setval",
    "txid_current",
    # remote access and dynamic SQL in disguise
    "dblink",
    "dblink_exec",
    "dblink_connect",
    "query_to_xml",
    "table_to_xml",
    "query_to_xml_and_xmlschema",
    "cursor_to_xml",
    "xpath",
    # server introspection
    "inet_server_addr",
    "inet_server_port",
    "version",
    "current_version",
    "pg_backend_pid",
}
DENY_PREFIXES = ("pg_", "lo_", "dblink", "xml")
DENY_SUBSTRINGS = ("generate_series", "sleep")  # sqlglot renames some (ExplodingGenerateSeries)


@dataclass
class GuardResult:
    ok: bool
    sql: str | None = None  # the rewritten, safe SQL (LIMIT enforced, comments stripped)
    reason: Reason | None = None
    detail: str = ""
    tables: set[str] = field(default_factory=set)

    @property
    def repairable(self) -> bool:
        return self.reason in REPAIRABLE


def _block(reason: Reason, detail: str) -> GuardResult:
    return GuardResult(ok=False, reason=reason, detail=detail)


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


class SqlGuard:
    def __init__(self, layer: SemanticLayer, max_rows: int = 500, max_chars: int = 20_000) -> None:
        self.layer = layer
        self.max_rows = max_rows
        self.max_chars = max_chars
        self.tables = set(layer.tables)
        self.columns = {c.lower() for c in layer.all_columns}
        self.restricted = {c.lower() for c in layer.restricted}

    def check(self, sql: str) -> GuardResult:
        if not sql or not sql.strip() or not sql.strip().strip(";").strip():
            return _block(Reason.EMPTY, "no SQL")
        if len(sql) > self.max_chars:
            return _block(Reason.TOO_LONG, f"SQL longer than {self.max_chars} characters")
        if "\x00" in sql:
            return _block(Reason.PARSE_ERROR, "NUL byte in SQL")
        try:
            stmts = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
        except (ParseError, TokenError) as e:
            return _block(Reason.PARSE_ERROR, str(e).splitlines()[0][:300])
        if len(stmts) != 1:
            return _block(
                Reason.MULTIPLE_STATEMENTS,
                f"{len(stmts)} statements; exactly one SELECT is allowed",
            )
        root = stmts[0]

        writes = [n for n in root.walk() if isinstance(n, WRITE_NODES)]
        if writes:
            return _block(
                Reason.WRITE_OPERATION, f"{type(writes[0]).__name__.upper()} is not allowed"
            )
        if not isinstance(root, exp.Query):
            return _block(
                Reason.NOT_SELECT, f"{type(root).__name__} statement; only SELECT is allowed"
            )
        for sel in root.find_all(exp.Select):
            if sel.args.get("into"):
                return _block(Reason.NOT_SELECT, "SELECT INTO creates a table")
            if sel.args.get("locks"):
                return _block(Reason.LOCKING, "FOR UPDATE/SHARE locks rows")

        ctes: set[str] = set()
        for w in root.find_all(exp.With):
            if w.args.get("recursive"):
                return _block(Reason.RECURSIVE_CTE, "recursive CTEs can run unbounded")
        for cte in root.find_all(exp.CTE):
            ctes.add(cte.alias_or_name.lower())

        for f in root.find_all(exp.Func):
            fname = _func_name(f)
            if (
                fname in DENY_FUNCTIONS
                or fname.startswith(DENY_PREFIXES)
                or any(x in fname for x in DENY_SUBSTRINGS)
            ):
                return _block(Reason.DISALLOWED_FUNCTION, f"function {fname}() is not allowed")

        used: set[str] = set()
        for t in root.find_all(exp.Table):
            name, db = t.name.lower(), (t.db or "").lower()
            if not name:  # table-valued function or similar
                return _block(Reason.DISALLOWED_TABLE, f"unsupported FROM item: {t.sql()[:80]}")
            if name in ctes and not db:
                continue
            if t.catalog or db not in ("", self.layer.schema) or name not in self.tables:
                full = ".".join(p for p in (t.catalog, t.db, t.name) if p)
                return _block(Reason.DISALLOWED_TABLE, f"{full} is not a curated view")
            used.add(name)

        aliases = {a.alias.lower() for a in root.find_all(exp.Alias) if a.alias}
        aliases |= {a.name.lower() for ta in root.find_all(exp.TableAlias) for a in ta.columns}
        aliases |= ctes
        for col in root.find_all(exp.Column):
            if isinstance(col.this, exp.Star):
                continue
            cname = col.name.lower()
            if cname in self.restricted:
                return _block(
                    Reason.DISALLOWED_COLUMN,
                    f"{cname} is restricted: {self.layer.restricted.get(cname, '')}",
                )
            if cname not in self.columns and cname not in aliases:
                return _block(Reason.UNKNOWN_COLUMN, f"column {cname} is not in the curated schema")

        sensitive = used & self.layer.sensitive_tables
        if sensitive:
            for star in root.find_all(exp.Star):
                if not isinstance(star.parent, exp.Count):
                    return _block(
                        Reason.STAR_ON_SENSITIVE,
                        f"SELECT * on {sorted(sensitive)[0]}; list the columns",
                    )

        for j in root.find_all(exp.Join):
            on, using = j.args.get("on"), j.args.get("using")
            if using:
                continue
            if on is None or not list(on.find_all(exp.Column)):
                return _block(
                    Reason.CARTESIAN_JOIN, f"join without a join condition: {j.sql()[:80]}"
                )

        limited = self._enforce_limit(root)
        if isinstance(limited, GuardResult):
            return limited
        return GuardResult(
            ok=True, sql=limited.sql(dialect="postgres", comments=False), tables=used
        )

    def _enforce_limit(self, root: exp.Query) -> exp.Query | GuardResult:
        if root.args.get("fetch") is not None:
            return _block(Reason.BAD_LIMIT, "use LIMIT instead of FETCH")
        lim = root.args.get("limit")
        if lim is None:
            return root.limit(self.max_rows, copy=True)
        value = lim.expression
        if not (isinstance(value, exp.Literal) and value.is_int):
            return _block(Reason.BAD_LIMIT, "LIMIT must be an integer literal")
        if int(value.this) > self.max_rows:
            return root.limit(self.max_rows, copy=True)
        return root
