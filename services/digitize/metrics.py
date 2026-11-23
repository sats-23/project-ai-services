"""
Prometheus metrics for the digitize service, exposed at ``GET /metrics``.

Everything except the HTTP counters is computed at scrape time by
``DigitizeCollector``, so nothing has to be instrumented in the pipeline:

  - process / host : CPU, memory, threads, file descriptors of the service and of
                     its Docling conversion worker processes (psutil)
  - conversion     : semaphore slots, tasks by status, queue wait, run time, pages
  - data           : jobs, documents and connectors by status
  - database       : SQLAlchemy connection-pool usage
  - HTTP           : request count and latency per route (middleware)

The DB-backed gauges run a handful of ``GROUP BY`` queries per scrape. If the
database is unreachable the scrape still succeeds and
``digitize_metrics_db_up`` reports 0.
"""

import time
from typing import Callable, Iterable

import psutil
from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from common.misc_utils import get_logger
from digitize.db.connection import engine, get_db_session
from digitize.workers.conversion_semaphore import conversion_semaphore

logger = get_logger("digitize_metrics")

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "digitize_http_requests",
    "HTTP requests by method, route template and status code",
    ["method", "path", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "digitize_http_request_duration_seconds",
    "HTTP request latency by method and route template",
    ["method", "path"],
    registry=REGISTRY,
)


def _sum(procs: Iterable[psutil.Process], read: Callable[[psutil.Process], float]) -> float:
    """Sum ``read(proc)`` over ``procs``, skipping processes that exited mid-scrape."""
    total = 0.0
    for proc in procs:
        try:
            total += read(proc)
        except psutil.Error:
            pass
    return total


def _gauge(name: str, doc: str, labels: list[str], samples: Iterable[tuple[list[str], float]]) -> GaugeMetricFamily:
    family = GaugeMetricFamily(name, doc, labels=labels)
    for label_values, value in samples:
        family.add_metric(label_values, value)
    return family


class DigitizeCollector:
    """Computes the service gauges each time Prometheus scrapes ``/metrics``."""

    def __init__(self) -> None:
        self._main = psutil.Process()
        # psutil.cpu_percent() reports usage since the previous call on the *same*
        # Process object, so keep the objects between scrapes. The first sample of a
        # new process is 0.
        self._workers: dict[int, psutil.Process] = {}
        self._main.cpu_percent(None)
        psutil.cpu_percent(None)

    def describe(self):
        # Empty describe() = unchecked collector; avoids running collect() (DB queries) at registration.
        return []

    def _worker_processes(self) -> list[psutil.Process]:
        live = {child.pid: child for child in self._main.children(recursive=True)}
        self._workers = {pid: self._workers.get(pid, child) for pid, child in live.items()}
        return list(self._workers.values())

    def collect(self):
        yield from self._process_metrics()
        yield from self._pipeline_metrics()
        yield from self._pool_metrics()

    # ------------------------------------------------------------------ #
    # Process / host                                                      #
    # ------------------------------------------------------------------ #

    def _process_metrics(self):
        scopes = {"main": [self._main], "workers": self._worker_processes()}

        def per_scope(read):
            return [([scope], _sum(procs, read)) for scope, procs in scopes.items()]

        yield _gauge(
            "digitize_process_cpu_percent",
            "CPU use of the service ('main') and its conversion workers; 100 = one full core",
            ["scope"], per_scope(lambda p: p.cpu_percent(None)),
        )
        yield _gauge(
            "digitize_process_memory_rss_bytes", "Resident memory", ["scope"],
            per_scope(lambda p: p.memory_info().rss),
        )
        yield _gauge("digitize_process_threads", "OS threads", ["scope"], per_scope(lambda p: p.num_threads()))
        yield _gauge(
            "digitize_process_open_fds", "Open file descriptors", ["scope"], per_scope(lambda p: p.num_fds()),
        )
        yield _gauge(
            "digitize_worker_processes", "Child processes of the service (Docling conversion workers + 1 multiprocessing resource tracker)",
            [], [([], len(scopes["workers"]))],
        )
        yield _gauge("digitize_host_cpu_percent", "Host-wide CPU use; 100 = all cores busy", [],
                     [([], psutil.cpu_percent(None))])
        yield _gauge("digitize_host_memory_used_percent", "Host-wide memory in use", [],
                     [([], psutil.virtual_memory().percent)])

    # ------------------------------------------------------------------ #
    # Conversion pipeline, jobs, documents, connectors (database)         #
    # ------------------------------------------------------------------ #

    def _pipeline_metrics(self):
        yield _gauge(
            "digitize_conversion_slots",
            "Conversion semaphore slots: 'capacity' (doc_worker_size) and 'used' (a large doc takes 2)",
            ["state"],
            [(["capacity"], conversion_semaphore.capacity),
             (["used"], conversion_semaphore.capacity - conversion_semaphore.available)],
        )
        try:
            families = self._query_database()
            db_up = 1
        except Exception as exc:
            logger.debug(f"Metrics database queries failed: {exc}")
            families, db_up = [], 0
        yield from families
        yield _gauge("digitize_metrics_db_up", "1 when the database-backed metrics were collected", [],
                     [([], db_up)])

    @staticmethod
    def _query_database() -> list[GaugeMetricFamily]:
        with get_db_session() as session:
            def rows(sql: str):
                return session.execute(text(sql)).all()

            def scalar(sql: str) -> float:
                return float(session.execute(text(sql)).scalar() or 0)

            def by_status(table: str):
                return [([status], n) for status, n in rows(f"SELECT status, COUNT(*) FROM {table} GROUP BY status")]

            return [
                _gauge(
                    "digitize_conversion_tasks", "Conversion tasks by status and operation", ["status", "operation"],
                    [([status, op], n) for status, op, n in
                     rows("SELECT status, operation, COUNT(*) FROM conversion_tasks GROUP BY status, operation")],
                ),
                _gauge(
                    "digitize_conversion_active_pages", "Pages in queued and running conversion tasks", ["status"],
                    [([status], n) for status, n in rows(
                        "SELECT status, COALESCE(SUM(page_count), 0) FROM conversion_tasks "
                        "WHERE status IN ('queued', 'running') GROUP BY status")],
                ),
                _gauge(
                    "digitize_conversion_oldest_queued_age_seconds", "Wait time of the oldest queued task", [],
                    [([], scalar("SELECT EXTRACT(EPOCH FROM now() - MIN(queued_at)) "
                                 "FROM conversion_tasks WHERE status = 'queued'"))],
                ),
                _gauge(
                    "digitize_conversion_longest_running_seconds", "Run time of the longest-running task", [],
                    [([], scalar("SELECT EXTRACT(EPOCH FROM now() - MIN(started_at)) "
                                 "FROM conversion_tasks WHERE status = 'running'"))],
                ),
                _gauge(
                    "digitize_conversion_avg_duration_seconds",
                    "Average run time of tasks completed in the last 15 minutes", [],
                    [([], scalar("SELECT AVG(EXTRACT(EPOCH FROM completed_at - started_at)) FROM conversion_tasks "
                                 "WHERE status = 'completed' AND completed_at > now() - interval '15 minutes'"))],
                ),
                _gauge("digitize_jobs", "Jobs by status", ["status"], by_status("jobs")),
                _gauge("digitize_documents", "Documents by status", ["status"], by_status("documents")),
                _gauge("digitize_connectors", "Connectors by status", ["status"], by_status("connectors")),
            ]

    # ------------------------------------------------------------------ #
    # Database connection pool                                            #
    # ------------------------------------------------------------------ #

    def _pool_metrics(self):
        pool = engine.pool if engine is not None else None
        samples = [] if pool is None else [
            (["size"], pool.size()),
            (["checked_out"], pool.checkedout()),
            (["overflow"], max(pool.overflow(), 0)),
        ]
        yield _gauge("digitize_db_pool_connections", "SQLAlchemy pool: configured size, in use, overflow",
                     ["state"], samples)


REGISTRY.register(DigitizeCollector())


async def metrics_middleware(request: Request, call_next):
    """Record request count and latency, labelled by route template (not raw URL)."""
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = route.path if route is not None else "unmatched"
    HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
    HTTP_LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
    return response


def render_metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
