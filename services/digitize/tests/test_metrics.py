"""Unit tests for digitize.metrics (the /metrics endpoint and its collector)."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client.parser import text_string_to_metric_families

import digitize.metrics as metrics


def _samples(payload: bytes) -> dict[str, dict[tuple, float]]:
    out: dict[str, dict[tuple, float]] = {}
    for family in text_string_to_metric_families(payload.decode()):
        for s in family.samples:
            out.setdefault(s.name, {})[tuple(sorted(s.labels.items()))] = s.value
    return out


def _fake_session(results: dict[str, list]):
    """Session whose execute() answers by matching a keyword of the SQL text."""
    session = MagicMock()

    def execute(clause):
        sql = str(clause)
        for keyword, value in results.items():
            if keyword in sql:
                result = MagicMock()
                result.all.return_value = value
                result.scalar.return_value = value
                return result
        raise AssertionError(f"unexpected SQL: {sql}")

    session.execute.side_effect = execute

    @contextmanager
    def get_db_session():
        yield session

    return get_db_session


@pytest.mark.unit
class TestCollector:
    def test_process_metrics_are_exposed_without_database(self):
        # No DB configured in unit tests: get_db_session is None -> db_up must be 0, scrape still works.
        with patch.object(metrics, "get_db_session", None):
            s = _samples(metrics.render_metrics().body)

        assert s["digitize_metrics_db_up"][()] == 0
        assert s["digitize_process_memory_rss_bytes"][(("scope", "main"),)] > 0
        assert s["digitize_process_threads"][(("scope", "main"),)] >= 1
        assert (("scope", "workers"),) in s["digitize_process_cpu_percent"]
        assert s["digitize_conversion_slots"][(("state", "capacity"),)] >= 1
        assert "digitize_conversion_tasks" not in s

    def test_database_metrics(self):
        session_cm = _fake_session({
            "GROUP BY status, operation": [("queued", "ingestion", 3), ("running", "digitization", 1)],
            "SUM(page_count)": [("running", 120)],
            "MIN(queued_at)": 42.5,
            "MIN(started_at)": 7.0,
            "AVG(": 12.0,
            "FROM jobs": [("in_progress", 2)],
            "FROM documents": [("completed", 9)],
            "FROM connectors": [("up to date", 1)],
        })
        with patch.object(metrics, "get_db_session", session_cm):
            s = _samples(metrics.render_metrics().body)

        assert s["digitize_metrics_db_up"][()] == 1
        tasks = s["digitize_conversion_tasks"]
        assert tasks[(("operation", "ingestion"), ("status", "queued"))] == 3
        assert tasks[(("operation", "digitization"), ("status", "running"))] == 1
        assert s["digitize_conversion_active_pages"][(("status", "running"),)] == 120
        assert s["digitize_conversion_oldest_queued_age_seconds"][()] == 42.5
        assert s["digitize_conversion_longest_running_seconds"][()] == 7.0
        assert s["digitize_conversion_avg_duration_seconds"][()] == 12.0
        assert s["digitize_jobs"][(("status", "in_progress"),)] == 2
        assert s["digitize_documents"][(("status", "completed"),)] == 9
        assert s["digitize_connectors"][(("status", "up to date"),)] == 1

    def test_pool_metrics(self):
        pool = MagicMock()
        pool.size.return_value = 5
        pool.checkedout.return_value = 2
        pool.overflow.return_value = -3  # SQLAlchemy reports negative until the pool is full
        with patch.object(metrics, "get_db_session", None), patch.object(metrics, "engine", MagicMock(pool=pool)):
            s = _samples(metrics.render_metrics().body)

        pool_metrics = s["digitize_db_pool_connections"]
        assert pool_metrics[(("state", "size"),)] == 5
        assert pool_metrics[(("state", "checked_out"),)] == 2
        assert pool_metrics[(("state", "overflow"),)] == 0


@pytest.mark.unit
class TestMiddleware:
    @pytest.mark.asyncio
    async def test_records_route_template_not_raw_url(self):
        request = MagicMock()
        request.method = "GET"
        request.scope = {"route": MagicMock(path="/v1/jobs/{job_id}")}
        response = MagicMock(status_code=200)

        async def call_next(_):
            return response

        before = _samples(metrics.render_metrics().body).get("digitize_http_requests_total", {})
        key = (("method", "GET"), ("path", "/v1/jobs/{job_id}"), ("status", "200"))

        assert await metrics.metrics_middleware(request, call_next) is response

        after = _samples(metrics.render_metrics().body)
        assert after["digitize_http_requests_total"][key] == before.get(key, 0) + 1
        assert (("method", "GET"), ("path", "/v1/jobs/{job_id}")) in after[
            "digitize_http_request_duration_seconds_count"
        ]
