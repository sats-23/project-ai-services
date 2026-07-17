"""
SFTP Poller — PoC.

Runs a background thread that wakes up every POLL_INTERVAL_SECONDS (default 300)
and performs the following diff cycle:

1. Opens an SFTP session.
2. Lists all supported files in the remote folder.
3. Checksums each file.
4. Compares against the in-memory state table (filename → SHA-256):
   - NEW file     → download + ingest.
   - CHANGED file → delete from vector store + re-ingest.
   - REMOVED file → delete from vector store + remove from state.
5. Updates the state table.
6. Saves the state table to a JSON file on disk so it survives a restart.

The poller is started / stopped via ``SFTPPoller.start()`` / ``SFTPPoller.stop()``.
A module-level singleton ``poller`` is exported for use in app.py and the API router.
"""

import asyncio
import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import common.db_utils as db
from common.misc_utils import get_logger, cleanup_staging_directory, get_utc_timestamp
from digitize.connectors.sftp import SFTPSession, config_summary
from digitize.models import DocStatus, JobStatus, OutputFormat, OperationType
from digitize.pipeline.ingest import ingest
from digitize.settings import settings
from digitize.utils.jobs import generate_uuid, initialize_job_state
from digitize.workers.concurrency import concurrency_manager

logger = get_logger("sftp_poller")

# ── Configuration ─────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS: int = 300  # 5 minutes

# Persistent state file — survives service restarts
_STATE_FILE: Path = settings.digitize.cache_dir / "sftp_poller_state.json"

# Temporary download directory — one sub-dir per poll cycle
_DOWNLOAD_BASE: Path = settings.digitize.cache_dir / "sftp_downloads"

# ── State helpers ─────────────────────────────────────────────────────────────


def _load_state() -> dict:
    """
    Load persisted poller state from disk.

    State schema::

        {
          "checksums": { "<filename>": "<sha256_hex>" },
          "doc_ids":   { "<filename>": "<doc_id_uuid>" }
        }
    """
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE) as fh:
                data = json.load(fh)
            logger.debug(f"Loaded poller state: {len(data.get('checksums', {}))} file(s)")
            return data
        except Exception as exc:
            logger.warning(f"Failed to read poller state file ({exc}); starting fresh")
    return {"checksums": {}, "doc_ids": {}}


def _save_state(state: dict) -> None:
    """Atomically write the poller state to disk."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    tmp.replace(_STATE_FILE)
    logger.debug("Poller state saved")


# ── Ingest helpers ────────────────────────────────────────────────────────────


def _delete_from_vdb(doc_id: str) -> None:
    """Remove all chunks for *doc_id* from the vector store."""
    try:
        vector_store = db.get_vector_store()
        deleted = vector_store.delete_document_by_id(doc_id)
        logger.info(f"VDB: removed {deleted} chunk(s) for doc {doc_id}")
    except Exception as exc:
        logger.error(f"VDB deletion failed for doc {doc_id}: {exc}", exc_info=True)


def _delete_from_db(doc_id: str) -> None:
    """Remove the PostgreSQL document record for *doc_id*."""
    try:
        from digitize.db.manager import db_manager as _db_mgr
        _db_mgr.delete_document(doc_id)
        logger.info(f"DB: removed document record {doc_id}")
    except Exception as exc:
        logger.error(f"DB deletion failed for doc {doc_id}: {exc}", exc_info=True)


def _run_ingest_sync(staging_path: Path, job_id: str, doc_id_dict: dict) -> None:
    """
    Blocking ingest call — runs in the poller thread (not the event loop).
    Mirrors what ``_run_ingest`` does in jobs.py but without async.
    """
    from digitize.utils.db import get_status_manager
    status_mgr = get_status_manager(job_id)
    try:
        logger.info(f"SFTP poller: starting ingest for job {job_id}")
        ingest(staging_path, job_id, doc_id_dict)
        logger.info(f"SFTP poller: ingest done for job {job_id}")
    except Exception as exc:
        logger.error(f"SFTP poller: ingest error for job {job_id}: {exc}", exc_info=True)
        status_mgr.update_job_progress(
            "",
            DocStatus.FAILED,
            JobStatus.FAILED,
            error=f"SFTP poller ingest error: {exc}",
        )
    finally:
        cleanup_staging_directory(job_id, settings.digitize.staging_dir)
        concurrency_manager.release("ingestion")


def _stage_files(job_id: str, files: list[Path]) -> Path:
    """
    Copy already-downloaded files from the temp download dir to the staging area.
    Returns the staging sub-directory path.
    """
    staging_path: Path = settings.digitize.staging_dir / job_id
    staging_path.mkdir(parents=True, exist_ok=True)
    for src in files:
        shutil.copy2(src, staging_path / src.name)
        logger.debug(f"Staged: {src.name}")
    return staging_path


def _ingest_files(filenames: list[str], local_paths: list[Path], job_name: str) -> dict[str, str]:
    """
    Create job + document records, stage files, then run the ingestion pipeline.

    Returns the doc_id_dict (filename → doc_id).
    """
    if not filenames:
        return {}

    job_id = generate_uuid()

    # Check / acquire the ingestion semaphore (blocking — poller runs in its own thread)
    if concurrency_manager.is_locked("ingestion"):
        logger.warning("SFTP poller: ingestion semaphore busy; skipping this cycle")
        return {}

    # initialize_job_state is synchronous — safe to call from the thread
    doc_id_dict = initialize_job_state(
        job_id=job_id,
        operation=OperationType.INGESTION,
        output_format=OutputFormat.JSON,
        documents_info=filenames,
        job_name=job_name,
    )

    # Acquire the semaphore *after* the job record is created
    # (avoids a race where another request sneaks in)
    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        pass

    # Semaphore acquisition from a sync context
    if loop and loop.is_running():
        # We're being called from an asyncio event loop thread — shouldn't happen
        # for the poller but guard defensively
        future = asyncio.run_coroutine_threadsafe(
            concurrency_manager.acquire("ingestion"), loop
        )
        future.result(timeout=10)
    else:
        # Poller runs in its own thread with no event loop — use blocking wait
        _blocking_acquire()

    staging_path = _stage_files(job_id, local_paths)
    _run_ingest_sync(staging_path, job_id, doc_id_dict)
    return doc_id_dict


def _blocking_acquire() -> None:
    """Busy-wait on the concurrency semaphore (poller thread, no event loop)."""
    while concurrency_manager.is_locked("ingestion"):
        logger.debug("SFTP poller: waiting for ingestion semaphore…")
        time.sleep(5)
    # Use the internal asyncio Semaphore from a fresh event loop
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(concurrency_manager.acquire("ingestion"))
    finally:
        loop.close()


# ── Main poll cycle ───────────────────────────────────────────────────────────


def _poll_once(state: dict) -> dict:
    """
    Execute one full poll cycle.

    Args:
        state: Current in-memory state (mutated in-place and returned).

    Returns:
        Updated state dict.
    """
    checksums: dict[str, str] = state.get("checksums", {})
    doc_ids: dict[str, str] = state.get("doc_ids", {})

    logger.info("SFTP poller: starting poll cycle")

    # ── Connect and diff ──────────────────────────────────────────────────────
    try:
        with SFTPSession() as sess:
            remote_files = sess.list_files()

            # Compute checksums for all remote files
            remote_checksums: dict[str, str] = {}
            for filename in remote_files:
                try:
                    remote_checksums[filename] = sess.checksum(filename)
                except Exception as exc:
                    logger.error(f"Checksum failed for {filename}: {exc}")

            # Detect new / changed / removed files
            new_files = [
                f for f in remote_checksums
                if f not in checksums
            ]
            changed_files = [
                f for f in remote_checksums
                if f in checksums and remote_checksums[f] != checksums[f]
            ]
            removed_files = [
                f for f in checksums
                if f not in remote_checksums
            ]

            logger.info(
                f"SFTP diff — new: {len(new_files)}, "
                f"changed: {len(changed_files)}, "
                f"removed: {len(removed_files)}"
            )

            # ── Handle removed files ──────────────────────────────────────────
            for filename in removed_files:
                doc_id = doc_ids.get(filename)
                if doc_id:
                    logger.info(f"Removing deleted remote file: {filename} (doc {doc_id})")
                    _delete_from_vdb(doc_id)
                    _delete_from_db(doc_id)
                    doc_ids.pop(filename, None)
                checksums.pop(filename, None)

            # ── Handle changed files — delete old, then re-ingest ─────────────
            if changed_files:
                for filename in changed_files:
                    old_doc_id = doc_ids.get(filename)
                    if old_doc_id:
                        logger.info(f"Removing stale chunks for changed file: {filename}")
                        _delete_from_vdb(old_doc_id)
                        _delete_from_db(old_doc_id)
                        doc_ids.pop(filename, None)
                    checksums.pop(filename, None)

            # ── Download new + changed files together ─────────────────────────
            to_ingest = new_files + changed_files
            if to_ingest:
                cycle_id = str(uuid.uuid4())[:8]
                download_dir = _DOWNLOAD_BASE / cycle_id
                try:
                    local_paths = sess.download(to_ingest, download_dir)
                    if local_paths:
                        job_name = f"sftp-poller-{cycle_id}"
                        new_doc_ids = _ingest_files(
                            filenames=[p.name for p in local_paths],
                            local_paths=local_paths,
                            job_name=job_name,
                        )
                        # Persist new doc IDs and checksums on success
                        for filename in to_ingest:
                            if filename in remote_checksums:
                                checksums[filename] = remote_checksums[filename]
                        for filename, doc_id in new_doc_ids.items():
                            doc_ids[filename] = doc_id
                finally:
                    if download_dir.exists():
                        shutil.rmtree(download_dir, ignore_errors=True)
            else:
                # Still update checksums for files that didn't change
                # (keeps state in sync if a previous cycle failed mid-way)
                for filename, digest in remote_checksums.items():
                    if filename not in checksums:
                        checksums[filename] = digest

    except Exception as exc:
        logger.error(f"SFTP poller cycle failed: {exc}", exc_info=True)

    state["checksums"] = checksums
    state["doc_ids"] = doc_ids
    return state


# ── Poller class ──────────────────────────────────────────────────────────────


class SFTPPoller:
    """
    Background thread that polls the SFTP remote folder on a fixed interval.

    Attributes
    ----------
    running : bool
        True while the polling loop is active.
    last_poll_at : Optional[str]
        ISO-8601 timestamp of the last completed poll cycle.
    last_error : Optional[str]
        Error message from the last failed cycle, or None.
    state : dict
        In-memory state (checksums + doc_ids).
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.running: bool = False
        self.last_poll_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.state: dict = _load_state()

    def start(self) -> None:
        """Start the background polling thread (idempotent)."""
        if self.running:
            logger.info("SFTP poller already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="sftp-poller",
            daemon=True,
        )
        self._thread.start()
        self.running = True
        logger.info(
            f"SFTP poller started (interval: {POLL_INTERVAL_SECONDS}s, "
            f"target: {config_summary()['host']}{config_summary()['remote_path']})"
        )

    def stop(self) -> None:
        """Signal the polling thread to stop and wait for it to exit."""
        if not self.running:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.running = False
        logger.info("SFTP poller stopped")

    def trigger_now(self) -> None:
        """
        Force an immediate poll on the *current* thread (blocking).
        Intended for the manual-trigger API endpoint.
        """
        logger.info("SFTP poller: manual trigger")
        try:
            self.state = _poll_once(self.state)
            _save_state(self.state)
            self.last_poll_at = get_utc_timestamp()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            logger.error(f"Manual trigger failed: {exc}", exc_info=True)

    def status(self) -> dict:
        """Return a JSON-serialisable status snapshot."""
        cfg = config_summary()
        return {
            "running": self.running,
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "tracked_files": len(self.state.get("checksums", {})),
            "config": cfg,
        }

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Main polling loop — runs in the background thread."""
        # Run an immediate first poll, then wait between cycles
        self._tick()

        while not self._stop_event.wait(timeout=POLL_INTERVAL_SECONDS):
            self._tick()

        logger.info("SFTP poller loop exiting")

    def _tick(self) -> None:
        """Execute one poll cycle and update internal state."""
        try:
            self.state = _poll_once(self.state)
            _save_state(self.state)
            self.last_poll_at = get_utc_timestamp()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            logger.error(f"SFTP poller tick failed: {exc}", exc_info=True)


# Module-level singleton — imported everywhere else
poller = SFTPPoller()
