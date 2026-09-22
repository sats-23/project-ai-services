"""
SQL Database Agent — wraps the safe SELECT executor in common/sql_utils.
Called by the orchestrator when the LLM chooses the `query_sql_database` tool.
"""
import json
from common.misc_utils import get_logger
from common.sql_utils import execute_select

logger = get_logger("sql_agent")

# Cap result rows returned to the LLM to keep context manageable
_MAX_ROWS = 50


def run(sql: str) -> str:
    """Execute a SELECT query and return results as a JSON string.

    Returns a JSON string (not a dict) so it can be dropped directly into
    the `content` field of the tool-result message sent back to the LLM.

    Args:
        sql: A PostgreSQL SELECT statement.

    Returns:
        JSON-encoded list of row dicts, or an error message string.
    """
    logger.info(f"SQL agent executing: {sql[:200]!r}")
    try:
        rows = execute_select(sql)
        if len(rows) > _MAX_ROWS:
            logger.info(f"Truncating SQL results from {len(rows)} to {_MAX_ROWS} rows")
            rows = rows[:_MAX_ROWS]
        return json.dumps(rows, default=str)
    except (ValueError, RuntimeError) as e:
        logger.warning(f"SQL agent error: {e}")
        return json.dumps({"error": str(e)})
