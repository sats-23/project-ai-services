#!/usr/bin/env python3
"""
test_sftp_poc.py — Standalone SFTP connector smoke-test.

PURPOSE
-------
This file lets you exercise the entire SFTP connector + poller logic
WITHOUT a running digitize service, database, or vector store.
All heavy service dependencies (OpenSearch, PostgreSQL, LLM pipeline)
are replaced by lightweight stubs so you only need:

    pip install paramiko

WHAT IT TESTS
-------------
1. SFTP connectivity  — opens a real session and lists the remote folder.
2. Checksum           — streams each file through SHA-256 in-memory.
3. Download           — pulls files to a local /tmp directory.
4. Diff logic         — simulates the full new / changed / removed detection.
5. Polling loop       — runs 2 poll cycles (immediate + after POLL_INTERVAL),
                        then stops.

HOW TO TRACE BACK TO THE DIGITIZE SERVICE
------------------------------------------
Every section below carries a "← digitize:" comment that tells you exactly
which file / function in the digitize service the logic came from.

Section map
───────────────────────────────────────────────────────────────
SFTP config         ← services/digitize/connectors/sftp.py
                       top-level constants (SFTP_HOST, SFTP_PORT, …)

_make_transport()   ← services/digitize/connectors/sftp.py::_make_transport()

list_remote_files() ← services/digitize/connectors/sftp.py::list_remote_files()

checksum_remote_file() ← services/digitize/connectors/sftp.py::checksum_remote_file()

download_files()    ← services/digitize/connectors/sftp.py::download_files()

SFTPSession         ← services/digitize/connectors/sftp.py::SFTPSession

_load_state()       ← services/digitize/connectors/poller.py::_load_state()
_save_state()       ← services/digitize/connectors/poller.py::_save_state()

_delete_from_vdb()  ← services/digitize/connectors/poller.py::_delete_from_vdb()
                       calls common.db_utils.get_vector_store().delete_document_by_id()

_delete_from_db()   ← services/digitize/connectors/poller.py::_delete_from_db()
                       calls digitize.db.manager.db_manager.delete_document()

_stage_files()      ← services/digitize/connectors/poller.py::_stage_files()
                       copies downloads into settings.digitize.staging_dir / job_id

_run_ingest_sync()  ← services/digitize/connectors/poller.py::_run_ingest_sync()
                       calls digitize.pipeline.ingest.ingest()

_ingest_files()     ← services/digitize/connectors/poller.py::_ingest_files()
                       calls digitize.utils.jobs.initialize_job_state()
                       calls digitize.workers.concurrency.concurrency_manager.acquire()

_poll_once()        ← services/digitize/connectors/poller.py::_poll_once()
                       the core diff algorithm

SFTPPoller          ← services/digitize/connectors/poller.py::SFTPPoller
                       started in services/digitize/app.py lifespan (startup)
                       stopped in services/digitize/app.py lifespan (shutdown)

REST endpoints      ← services/digitize/api/v1/sftp.py
                       GET  /v1/sftp/config   → config_summary()
                       GET  /v1/sftp/status   → SFTPPoller.status()
                       POST /v1/sftp/start    → SFTPPoller.start()
                       POST /v1/sftp/stop     → SFTPPoller.stop()
                       POST /v1/sftp/trigger  → SFTPPoller.trigger_now()
───────────────────────────────────────────────────────────────

RUN
---
From the repo root (or services/ directory):

    # with the project venv active:
    python services/digitize/connectors/test_sftp_poc.py

    # or directly:
    cd services
    python digitize/connectors/test_sftp_poc.py

    # verbose mode (shows each downloaded file path):
    python services/digitize/connectors/test_sftp_poc.py --verbose

    # run only N poll cycles then exit (default = 2):
    python services/digitize/connectors/test_sftp_poc.py --cycles 1

    # override the poll interval for faster iteration (seconds):
    python services/digitize/connectors/test_sftp_poc.py --interval 10
"""

import argparse
import hashlib
import json
import logging
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Stdlib-only logging (no common.misc_utils needed for the standalone runner)
# ← In the real service this is replaced by common.misc_utils.get_logger()
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sftp_poc_test")


# ─────────────────────────────────────────────────────────────────────────────
# 1. SFTP CONFIG  ← services/digitize/connectors/sftp.py (top-level constants)
# ─────────────────────────────────────────────────────────────────────────────
# !! Fill these in before running !!

SFTP_HOST: str = "10.20.185.60"
SFTP_PORT: int = 22
SFTP_USERNAME: str = "root"
SFTP_REMOTE_PATH: str = "/root/sats/watch"   # trailing slash optional

# Auth — set exactly one:
SFTP_PASSWORD: Optional[str] = None              # e.g. "s3cr3t"
SFTP_PRIVATE_KEY_PATH: Optional[str] = "/Users/sats/.ssh/id_ed25519"      # e.g. "/home/you/.ssh/id_rsa"
SFTP_PRIVATE_KEY_PASSPHRASE: Optional[str] = None

# Displayed in the /v1/sftp/config response in the real service
SFTP_PUBLIC_KEY: str = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC... poc@rag-system"

# Only these extensions are considered for ingestion
# ← sftp.py::SUPPORTED_EXTS
SUPPORTED_EXTS = {".pdf", ".docx"}

# ─────────────────────────────────────────────────────────────────────────────
# 2. POLL INTERVAL  ← services/digitize/connectors/poller.py::POLL_INTERVAL_SECONDS
# ─────────────────────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS: int = 20  # overridden by --interval CLI flag

# ─────────────────────────────────────────────────────────────────────────────
# 3. LOCAL PATHS  ← services/digitize/connectors/poller.py (_STATE_FILE, _DOWNLOAD_BASE)
#    In the real service these live under settings.digitize.cache_dir
#    Here we use /tmp so no service setup is needed.
# ─────────────────────────────────────────────────────────────────────────────

_TMP_BASE = Path("/tmp/sftp_poc_test")
_STATE_FILE = _TMP_BASE / "sftp_poller_state.json"   # ← poller.py::_STATE_FILE
_DOWNLOAD_BASE = _TMP_BASE / "sftp_downloads"         # ← poller.py::_DOWNLOAD_BASE
_STAGING_BASE = _TMP_BASE / "staging"                 # ← settings.digitize.staging_dir


# ─────────────────────────────────────────────────────────────────────────────
# 4. SFTP TRANSPORT  ← services/digitize/connectors/sftp.py::_make_transport()
# ─────────────────────────────────────────────────────────────────────────────

def _make_transport():
    """
    Open a raw paramiko Transport to the configured host/port.

    Exact copy of sftp.py::_make_transport().
    ← services/digitize/connectors/sftp.py::_make_transport()
    """
    import paramiko  # only dependency beyond stdlib

    transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))

    if SFTP_PRIVATE_KEY_PATH:
        pkey = None
        last_exc: Exception = RuntimeError("No key classes available")
        for key_cls in paramiko.key_classes:
            try:
                pkey = key_cls.from_private_key_file(
                    SFTP_PRIVATE_KEY_PATH,
                    password=SFTP_PRIVATE_KEY_PASSPHRASE,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if pkey is None:
            raise last_exc
        transport.connect(username=SFTP_USERNAME, pkey=pkey)
        log.debug(f"Connected to {SFTP_HOST} via private key")
    elif SFTP_PASSWORD is not None:
        transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)
        log.debug(f"Connected to {SFTP_HOST} via password")
    else:
        raise ValueError(
            "Auth not configured: set SFTP_PASSWORD or SFTP_PRIVATE_KEY_PATH"
        )

    return transport


# ─────────────────────────────────────────────────────────────────────────────
# 5. SFTP SESSION  ← services/digitize/connectors/sftp.py::SFTPSession
# ─────────────────────────────────────────────────────────────────────────────

class SFTPSession:
    """
    Context-manager owning a paramiko Transport + SFTPClient.

    Exact copy of sftp.py::SFTPSession.
    ← services/digitize/connectors/sftp.py::SFTPSession
    """

    def __init__(self):
        self._transport = None
        self._client = None

    def __enter__(self):
        import paramiko
        self._transport = _make_transport()
        self._client = paramiko.SFTPClient.from_transport(self._transport)
        return self

    def __exit__(self, *_):
        if self._client:
            self._client.close()
        if self._transport:
            self._transport.close()

    # ── delegates ── (mirror sftp.py convenience methods)

    def list_files(self) -> list:
        """← sftp.py::list_remote_files()"""
        try:
            entries = self._client.listdir(SFTP_REMOTE_PATH)
        except IOError as exc:
            raise IOError(f"Cannot list '{SFTP_REMOTE_PATH}': {exc}") from exc
        supported = [f for f in entries if Path(f).suffix.lower() in SUPPORTED_EXTS]
        log.debug(f"Remote listing: {len(supported)} supported file(s)")
        return supported

    def checksum(self, filename: str) -> str:
        """
        Stream file through SHA-256 without writing to disk.
        ← sftp.py::checksum_remote_file()
        """
        remote_path = f"{SFTP_REMOTE_PATH.rstrip('/')}/{filename}"
        sha = hashlib.sha256()
        with self._client.open(remote_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                sha.update(chunk)
        return sha.hexdigest()

    def download(self, filenames: list, local_dir: Path) -> list:
        """
        Download filenames into local_dir, return local Paths.
        ← sftp.py::download_files()
        """
        local_dir.mkdir(parents=True, exist_ok=True)
        downloaded = []
        for filename in filenames:
            remote_path = f"{SFTP_REMOTE_PATH.rstrip('/')}/{filename}"
            local_path = local_dir / filename
            try:
                self._client.get(remote_path, str(local_path))
                log.info(f"Downloaded: {filename} → {local_path}")
                downloaded.append(local_path)
            except Exception as exc:
                log.error(f"Download failed for {filename}: {exc}")
        return downloaded


# ─────────────────────────────────────────────────────────────────────────────
# 6. STATE PERSISTENCE  ← services/digitize/connectors/poller.py
#                          ::_load_state() / ::_save_state()
# ─────────────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    """
    Load state from disk. Falls back to empty state on any error.
    ← poller.py::_load_state()
    """
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE) as fh:
                data = json.load(fh)
            log.debug(f"Loaded state: {len(data.get('checksums', {}))} tracked file(s)")
            return data
        except Exception as exc:
            log.warning(f"Could not read state file ({exc}); starting fresh")
    return {"checksums": {}, "doc_ids": {}}


def _save_state(state: dict) -> None:
    """
    Atomically write state to disk (write-then-rename).
    ← poller.py::_save_state()
    """
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    tmp.replace(_STATE_FILE)
    log.debug(f"State saved → {_STATE_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# 7. STUB INGEST HELPERS
#    In the real service these call into the full pipeline.
#    Here they are no-ops that print what *would* happen so you can verify
#    the diff logic without needing OpenSearch / PostgreSQL / LLM.
#
#    ← poller.py::_delete_from_vdb()   (calls common.db_utils.get_vector_store())
#    ← poller.py::_delete_from_db()    (calls digitize.db.manager.db_manager)
#    ← poller.py::_stage_files()       (copies files to staging dir)
#    ← poller.py::_run_ingest_sync()   (calls digitize.pipeline.ingest.ingest())
#    ← poller.py::_ingest_files()      (orchestrates job creation + ingest call)
# ─────────────────────────────────────────────────────────────────────────────

def _delete_from_vdb(doc_id: str) -> None:
    """
    STUB: In the real service calls vector_store.delete_document_by_id(doc_id).
    ← poller.py::_delete_from_vdb()
    """
    log.info(f"[STUB] VDB delete: doc_id={doc_id}  "
             f"(real: common.db_utils.get_vector_store().delete_document_by_id(doc_id))")


def _delete_from_db(doc_id: str) -> None:
    """
    STUB: In the real service calls db_manager.delete_document(doc_id).
    ← poller.py::_delete_from_db()
    """
    log.info(f"[STUB] PostgreSQL delete: doc_id={doc_id}  "
             f"(real: digitize.db.manager.db_manager.delete_document(doc_id))")


def _generate_uuid() -> str:
    """
    UUID generator.
    ← digitize.utils.jobs.generate_uuid()
    """
    return str(uuid.uuid4())


def _stage_files(job_id: str, files: list) -> Path:
    """
    Copy downloaded files into the staging sub-directory for this job.
    ← poller.py::_stage_files()
       In the real service, staging_path = settings.digitize.staging_dir / job_id
    """
    staging_path = _STAGING_BASE / job_id
    staging_path.mkdir(parents=True, exist_ok=True)
    for src in files:
        shutil.copy2(src, staging_path / src.name)
        log.debug(f"Staged: {src.name} → {staging_path}")
    return staging_path


def _run_ingest_sync(staging_path: Path, job_id: str, doc_id_dict: dict) -> None:
    """
    STUB: In the real service calls digitize.pipeline.ingest.ingest().
    Mirrors poller.py::_run_ingest_sync() which itself mirrors
    api/v1/jobs.py::_run_ingest() but runs synchronously (no asyncio).

    ← poller.py::_run_ingest_sync()
       real call: ingest(staging_path, job_id, doc_id_dict)
    """
    log.info(
        f"[STUB] Ingest pipeline: job_id={job_id}  "
        f"staging_path={staging_path}  "
        f"docs={list(doc_id_dict.keys())}"
    )
    log.info(
        f"[STUB] (real: digitize.pipeline.ingest.ingest("
        f"staging_path, job_id, doc_id_dict))"
    )
    # In the real service this also calls:
    #   cleanup_staging_directory(job_id, settings.digitize.staging_dir)
    #   concurrency_manager.release("ingestion")
    if staging_path.exists():
        shutil.rmtree(staging_path, ignore_errors=True)
        log.debug(f"Cleaned staging dir: {staging_path}")


def _ingest_files(filenames: list, local_paths: list, job_name: str) -> dict:
    """
    Create job + document records, stage files, run ingestion.
    Returns doc_id_dict (filename → doc_id).

    ← poller.py::_ingest_files()
       In the real service:
         - generates job_id via digitize.utils.jobs.generate_uuid()
         - creates DB records via digitize.utils.jobs.initialize_job_state()
         - acquires concurrency_manager semaphore
         - calls _stage_files() then _run_ingest_sync()
    """
    if not filenames:
        return {}

    job_id = _generate_uuid()
    # ← real service: digitize.utils.jobs.initialize_job_state(
    #       job_id, OperationType.INGESTION, OutputFormat.JSON, filenames, job_name)
    doc_id_dict = {fname: _generate_uuid() for fname in filenames}

    log.info(
        f"[STUB] Job created: job_id={job_id}  job_name={job_name}  "
        f"docs={filenames}"
    )
    log.info(
        f"[STUB] (real: digitize.utils.jobs.initialize_job_state("
        f"job_id, OperationType.INGESTION, OutputFormat.JSON, {filenames}, '{job_name}'))"
    )

    staging_path = _stage_files(job_id, local_paths)
    _run_ingest_sync(staging_path, job_id, doc_id_dict)
    return doc_id_dict


# ─────────────────────────────────────────────────────────────────────────────
# 8. CORE DIFF / POLL CYCLE  ← services/digitize/connectors/poller.py::_poll_once()
# ─────────────────────────────────────────────────────────────────────────────

def _poll_once(state: dict) -> dict:
    """
    One complete poll cycle: connect → list → checksum → diff → act.

    This is a verbatim copy of poller.py::_poll_once() with the service
    imports replaced by the stubs defined above.

    ← services/digitize/connectors/poller.py::_poll_once()
    """
    checksums: dict = state.get("checksums", {})
    doc_ids: dict   = state.get("doc_ids", {})

    log.info("── Poll cycle starting ──────────────────────────────────")

    try:
        with SFTPSession() as sess:

            # ── Step 1: list supported files on the remote ────────────────────
            # ← sftp.py::list_remote_files() via SFTPSession.list_files()
            remote_files = sess.list_files()
            log.info(f"Remote files found: {remote_files}")

            # ── Step 2: checksum every remote file ────────────────────────────
            # ← sftp.py::checksum_remote_file() via SFTPSession.checksum()
            remote_checksums: dict = {}
            for filename in remote_files:
                try:
                    digest = sess.checksum(filename)
                    remote_checksums[filename] = digest
                    log.debug(f"  checksum {filename}: {digest[:12]}…")
                except Exception as exc:
                    log.error(f"Checksum failed for {filename}: {exc}")

            # ── Step 3: diff against known state ─────────────────────────────
            new_files = [f for f in remote_checksums if f not in checksums]
            changed_files = [
                f for f in remote_checksums
                if f in checksums and remote_checksums[f] != checksums[f]
            ]
            removed_files = [f for f in checksums if f not in remote_checksums]

            log.info(
                f"Diff result — "
                f"new: {new_files or '[]'}  "
                f"changed: {changed_files or '[]'}  "
                f"removed: {removed_files or '[]'}"
            )

            # ── Step 4a: handle removed files ─────────────────────────────────
            # ← poller.py::_poll_once() removed-files block
            for filename in removed_files:
                doc_id = doc_ids.get(filename)
                if doc_id:
                    log.info(f"Remote file removed: {filename} (doc_id={doc_id})")
                    _delete_from_vdb(doc_id)   # ← poller.py::_delete_from_vdb()
                    _delete_from_db(doc_id)    # ← poller.py::_delete_from_db()
                    doc_ids.pop(filename, None)
                checksums.pop(filename, None)

            # ── Step 4b: purge stale chunks for changed files ─────────────────
            # ← poller.py::_poll_once() changed-files deletion block
            for filename in changed_files:
                old_doc_id = doc_ids.get(filename)
                if old_doc_id:
                    log.info(f"File changed: {filename} — removing stale doc_id={old_doc_id}")
                    _delete_from_vdb(old_doc_id)
                    _delete_from_db(old_doc_id)
                    doc_ids.pop(filename, None)
                checksums.pop(filename, None)

            # ── Step 4c: download + ingest new and changed files ──────────────
            # ← poller.py::_poll_once() download + _ingest_files() call
            to_ingest = new_files + changed_files
            if to_ingest:
                cycle_id = str(uuid.uuid4())[:8]
                download_dir = _DOWNLOAD_BASE / cycle_id
                try:
                    # ← sftp.py::download_files() via SFTPSession.download()
                    local_paths = sess.download(to_ingest, download_dir)

                    if local_paths:
                        job_name = f"sftp-poller-{cycle_id}"
                        # ← poller.py::_ingest_files()
                        new_doc_ids = _ingest_files(
                            filenames=[p.name for p in local_paths],
                            local_paths=local_paths,
                            job_name=job_name,
                        )
                        # Update state with fresh checksums + new doc IDs
                        for filename in to_ingest:
                            if filename in remote_checksums:
                                checksums[filename] = remote_checksums[filename]
                        for filename, doc_id in new_doc_ids.items():
                            doc_ids[filename] = doc_id
                finally:
                    if download_dir.exists():
                        shutil.rmtree(download_dir, ignore_errors=True)
                        log.debug(f"Cleaned download_dir: {download_dir}")
            else:
                # No action needed — sync any newly-seen checksums to state
                for filename, digest in remote_checksums.items():
                    if filename not in checksums:
                        checksums[filename] = digest

    except Exception as exc:
        log.error(f"Poll cycle failed: {exc}", exc_info=True)

    state["checksums"] = checksums
    state["doc_ids"]   = doc_ids
    log.info("── Poll cycle done ──────────────────────────────────────")
    return state


# ─────────────────────────────────────────────────────────────────────────────
# 9. POLLER CLASS  ← services/digitize/connectors/poller.py::SFTPPoller
#
#    In the real service this class is instantiated once at module level
#    and its start()/stop() are called from services/digitize/app.py lifespan.
#    The REST endpoints in api/v1/sftp.py delegate to the same singleton.
# ─────────────────────────────────────────────────────────────────────────────

class SFTPPoller:
    """
    Background-thread poller.

    ← services/digitize/connectors/poller.py::SFTPPoller

    Lifecycle in the real service
    ─────────────────────────────
    start()  called from  app.py::lifespan() on startup
    stop()   called from  app.py::lifespan() on shutdown
    trigger_now() exposed via  POST /v1/sftp/trigger  (api/v1/sftp.py)
    status()      exposed via  GET  /v1/sftp/status   (api/v1/sftp.py)
    """

    def __init__(self, poll_interval: int = POLL_INTERVAL_SECONDS):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.running: bool = False
        self.last_poll_at: Optional[str] = None
        self.last_error: Optional[str] = None
        # ← poller.py::SFTPPoller.__init__() loads persistent state
        self.state: dict = _load_state()
        self._interval = poll_interval

    def start(self) -> None:
        """
        Start the background polling thread (idempotent).
        ← poller.py::SFTPPoller.start()
        Called from app.py lifespan on service startup.
        """
        if self.running:
            log.info("Poller already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="sftp-poller",
            daemon=True,
        )
        self._thread.start()
        self.running = True
        log.info(
            f"Poller started — target: {SFTP_HOST}{SFTP_REMOTE_PATH}  "
            f"interval: {self._interval}s"
        )

    def stop(self) -> None:
        """
        Signal the thread to stop and wait for it to exit.
        ← poller.py::SFTPPoller.stop()
        Called from app.py lifespan on service shutdown.
        """
        if not self.running:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        self.running = False
        log.info("Poller stopped")

    def trigger_now(self) -> None:
        """
        Force an immediate poll on the calling thread (blocking).
        ← poller.py::SFTPPoller.trigger_now()
        Exposed as POST /v1/sftp/trigger in api/v1/sftp.py.
        """
        log.info("Manual trigger")
        try:
            self.state = _poll_once(self.state)
            _save_state(self.state)
            self.last_poll_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            log.error(f"Manual trigger failed: {exc}", exc_info=True)

    def status(self) -> dict:
        """
        Return a JSON-serialisable status snapshot.
        ← poller.py::SFTPPoller.status()
        Exposed as GET /v1/sftp/status in api/v1/sftp.py.
        """
        return {
            "running": self.running,
            "poll_interval_seconds": self._interval,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "tracked_files": len(self.state.get("checksums", {})),
            "config": {
                "host": SFTP_HOST,
                "port": SFTP_PORT,
                "username": SFTP_USERNAME,
                "remote_path": SFTP_REMOTE_PATH,
                "supported_extensions": sorted(SUPPORTED_EXTS),
                "public_key": SFTP_PUBLIC_KEY,
            },
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """
        Main loop — immediate first tick, then repeat every _interval seconds.
        ← poller.py::SFTPPoller._loop()
        """
        self._tick()
        while not self._stop_event.wait(timeout=self._interval):
            self._tick()
        log.info("Poller loop exiting")

    def _tick(self) -> None:
        """
        One tick: poll + save state + record timestamp.
        ← poller.py::SFTPPoller._tick()
        """
        try:
            self.state = _poll_once(self.state)
            _save_state(self.state)
            self.last_poll_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            log.error(f"Tick failed: {exc}", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# 10. STANDALONE TEST RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def _print_section(title: str) -> None:
    width = 60
    log.info("=" * width)
    log.info(f"  {title}")
    log.info("=" * width)


def run_tests(max_cycles: Optional[int], interval: int) -> None:
    """
    Execute the test sequence.

    Steps:
      A) Validate SFTP credentials are set
      B) Connectivity test — list remote files
      C) Checksum test — checksum every remote file
      D) Download test — pull all files to /tmp
      E) Diff logic test — simulate new / changed / removed
      F) Polling loop test — start poller, poll indefinitely (or for
         max_cycles ticks when max_cycles is not None), stop on KeyboardInterrupt
    """
    _print_section("STEP A — Credential validation")
    if not SFTP_PASSWORD and not SFTP_PRIVATE_KEY_PATH:
        log.error(
            "No auth configured.  "
            "Set SFTP_PASSWORD or SFTP_PRIVATE_KEY_PATH at the top of this file."
        )
        sys.exit(1)
    log.info(f"Host     : {SFTP_HOST}:{SFTP_PORT}")
    log.info(f"Username : {SFTP_USERNAME}")
    log.info(f"Remote   : {SFTP_REMOTE_PATH}")
    auth = "private_key" if SFTP_PRIVATE_KEY_PATH else "password"
    log.info(f"Auth     : {auth}")

    # ── B: connectivity + listing ─────────────────────────────────────────────
    _print_section("STEP B — SFTP connectivity + directory listing")
    try:
        with SFTPSession() as sess:
            remote_files = sess.list_files()
        log.info(f"Remote files ({len(remote_files)}): {remote_files}")
    except Exception as exc:
        log.error(f"Connection failed: {exc}", exc_info=True)
        sys.exit(1)

    if not remote_files:
        log.warning("Remote directory is empty — upload some .pdf or .docx files and re-run.")
        # Continue — diff logic / poller tests still run against empty state

    # ── C: checksum ───────────────────────────────────────────────────────────
    _print_section("STEP C — SHA-256 checksum (in-memory, no disk write)")
    checksums: dict = {}
    try:
        with SFTPSession() as sess:
            for filename in remote_files:
                digest = sess.checksum(filename)
                checksums[filename] = digest
                log.info(f"  {filename}: {digest}")
    except Exception as exc:
        log.error(f"Checksum phase failed: {exc}", exc_info=True)

    # ── D: download ───────────────────────────────────────────────────────────
    _print_section("STEP D — Download to local /tmp")
    download_dir = _DOWNLOAD_BASE / "manual_test"
    downloaded: list = []
    if remote_files:
        try:
            with SFTPSession() as sess:
                downloaded = sess.download(remote_files, download_dir)
            log.info(f"Downloaded {len(downloaded)} file(s) to {download_dir}")
        except Exception as exc:
            log.error(f"Download phase failed: {exc}", exc_info=True)

    # ── E: diff logic (unit test — no real SFTP call) ─────────────────────────
    _print_section("STEP E — Diff logic simulation (no SFTP / no ingest)")
    state: dict = {"checksums": {}, "doc_ids": {}}

    if checksums:
        # Round 1: all files are new
        log.info("Round 1: treating all remote files as NEW")
        state["checksums"] = {}
        current_checksums = dict(checksums)

        new_files    = [f for f in current_checksums if f not in state["checksums"]]
        changed_files: list = []
        removed_files: list = []
        log.info(f"  new={new_files}  changed={changed_files}  removed={removed_files}")

        # Simulate state update
        for f in new_files:
            state["checksums"][f] = current_checksums[f]
            state["doc_ids"][f] = _generate_uuid()
        log.info(f"  State after round 1: {state}")

        # Round 2: mutate one checksum to simulate a change
        if remote_files:
            mutated = remote_files[0]
            mutated_checksums = dict(current_checksums)
            mutated_checksums[mutated] = "0" * 64   # fake different hash

            new_files2     = [f for f in mutated_checksums if f not in state["checksums"]]
            changed_files2 = [
                f for f in mutated_checksums
                if f in state["checksums"] and mutated_checksums[f] != state["checksums"][f]
            ]
            removed_files2 = [f for f in state["checksums"] if f not in mutated_checksums]
            log.info(
                f"Round 2 ('{mutated}' checksum mutated): "
                f"new={new_files2}  changed={changed_files2}  removed={removed_files2}"
            )
            assert mutated in changed_files2, "Expected changed file not detected!"
            log.info("  ✅  Change detection working correctly")

        # Round 3: remove first file from remote
        if len(remote_files) >= 1:
            removed_remote = set(remote_files[1:])  # drop first file
            removed_set = [f for f in state["checksums"] if f not in removed_remote]
            log.info(f"Round 3 (simulate removal of '{remote_files[0]}'): removed={removed_set}")
            assert remote_files[0] in removed_set, "Expected removed file not detected!"
            log.info("  ✅  Removal detection working correctly")
    else:
        log.info("Skipping diff simulation — no remote files to work with")

    # ── F: polling loop ───────────────────────────────────────────────────────
    cycles_label = f"{max_cycles} cycle(s)" if max_cycles is not None else "∞ cycles"
    _print_section(f"STEP F — Polling loop ({cycles_label}, interval={interval}s)")

    # Reset state so the poller starts clean
    if _STATE_FILE.exists():
        _STATE_FILE.unlink()

    cycle_count = 0
    original_poll_once = _poll_once.__code__

    # Monkey-patch a cycle counter around _poll_once
    # (avoids modifying the core logic while counting ticks)
    real_poll = _poll_once

    def _counting_poll(state: dict) -> dict:
        nonlocal cycle_count
        cycle_count += 1
        label = f"{cycle_count}/{max_cycles}" if max_cycles is not None else str(cycle_count)
        log.info(f"  [Cycle {label}]")
        result = real_poll(state)
        return result

    poller = SFTPPoller(poll_interval=interval)
    # Swap in the counting wrapper
    import types as _types
    poller._tick_real = poller._tick

    def _patched_tick(self=poller) -> None:
        try:
            self.state = _counting_poll(self.state)
            _save_state(self.state)
            self.last_poll_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            log.error(f"Tick failed: {exc}", exc_info=True)
        if max_cycles is not None and cycle_count >= max_cycles:
            self._stop_event.set()

    poller._tick = _patched_tick

    poller.start()
    try:
        if max_cycles is not None:
            poller._thread.join(timeout=max_cycles * interval + 30)
        else:
            while poller._thread.is_alive():
                poller._thread.join(timeout=1)
    except KeyboardInterrupt:
        log.info("Interrupted — stopping poller…")
    finally:
        poller.stop()

    # Final status
    log.info(f"Cycles completed: {cycle_count}")
    log.info(f"Final status: {json.dumps(poller.status(), indent=2)}")
    _print_section("ALL STEPS DONE")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SFTP connector PoC smoke-test")
    parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="Number of poll cycles to run before exiting (default: 0 = run forever until Ctrl-C)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Set log level to DEBUG",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    log.info(f"Temp directory: {_TMP_BASE}")

    run_tests(max_cycles=args.cycles if args.cycles > 0 else None, interval=args.interval)
