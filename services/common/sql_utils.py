"""
Safe, read-only SQL query executor for the Agentic RAG SQL agent.
Reuses the existing PostgreSQL engine from common/db/connection.py.
"""
import re
from typing import Any

from sqlalchemy import text

from common.misc_utils import get_logger

logger = get_logger("sql_utils")

# Only SELECT statements are allowed
_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

# Cached engine (lazy-initialised)
_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from common.db.connection import create_db_engine
        _engine = create_db_engine(logger_name="sql_utils")
    return _engine


def get_schema_summary() -> str:
    """Return a compact text summary of all user tables and their columns.

    This is injected into the orchestrator system prompt so the LLM knows
    what tables and columns exist before it writes SQL.
    """
    engine = _get_engine()
    query = text("""
        SELECT
            t.table_name,
            string_agg(c.column_name || ' ' || c.data_type, ', ' ORDER BY c.ordinal_position) AS columns
        FROM information_schema.tables t
        JOIN information_schema.columns c
            ON t.table_name = c.table_name AND t.table_schema = c.table_schema
        WHERE t.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
        GROUP BY t.table_name
        ORDER BY t.table_name;
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(query).fetchall()
        if not rows:
            return "No tables found in the public schema."
        lines = [f"- {row[0]}: {row[1]}" for row in rows]
        return "Available tables:\n" + "\n".join(lines)
    except Exception as e:
        logger.warning(f"Could not fetch schema summary: {e}")
        return "Schema information unavailable."


def execute_select(sql: str) -> list[dict[str, Any]]:
    """Execute a read-only SELECT query and return results as a list of dicts.

    Raises:
        ValueError: If the SQL is not a SELECT statement.
        RuntimeError: If the query fails.
    """
    if not _SELECT_RE.match(sql):
        raise ValueError(f"Only SELECT statements are permitted. Got: {sql[:80]!r}")

    engine = _get_engine()
    logger.info(f"Executing SQL: {sql[:200]}")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            keys = list(result.keys())
            rows = [dict(zip(keys, row)) for row in result.fetchall()]
        logger.info(f"SQL returned {len(rows)} rows")
        return rows
    except Exception as e:
        logger.error(f"SQL execution failed: {e}")
        raise RuntimeError(f"SQL query failed: {e}") from e
