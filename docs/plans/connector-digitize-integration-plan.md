# Connector → Digitize Integration Plan

## Top-Level Overview

**Goal:** Implement the digitize integration logic inside the `ConnectorSyncService` sync loop.
After the SFTP scanner produces a `ScanResult`, three cases must be handled:

1. **New file** (`ToIngest`) → stream the file to digitize via `POST /v1/jobs?operation=ingestion`.
2. **Modified file** (`ToReIngest`) → look up the existing document by filename in digitize
   (`GET /v1/documents?name=<filename>`), call `DELETE /v1/documents/{doc_id}`, then re-submit
   the updated file via `POST /v1/jobs?operation=ingestion`.
3. **Deleted file** (`ToRemove`) → look up the document by filename in digitize, call
   `DELETE /v1/documents/{doc_id}`, then delete the corresponding `ConnectorFileChecksum` record.

**Assumptions:**
- The `ConnectorSyncService` ticker and `SSHConnector.Scan()` already exist (sub-tasks 4 is done).
- The `ConnectorFileChecksumRepository` already exists (sub-task 1 is done).
- `doc_id` lookup by filename uses `GET /v1/documents?name={filename}` (Option B — no `doc_id`
  stored in the registry).
- The digitize base URL for an attached application is resolved from the application's deployed
  service endpoints (already available in the `Service` model).

**Scope:** Only the file-handling logic inside the sync loop — no new ticker, scanner, or
repo interfaces are introduced here.

---

## Sub-Tasks

### 1. Add a `DigitizeSyncClient` for connector-driven ingest and delete

- **Intent** — Create a focused HTTP client in the connectors package that wraps the two
  digitize operations needed by the sync service: (a) submit a file for ingestion and (b) delete
  a document by name. This keeps all digitize HTTP concerns in one place and mirrors the pattern
  in [`ai-services/internal/pkg/application/common/backup/digitize.go`](ai-services/internal/pkg/application/common/backup/digitize.go).
- **Expected Outcomes** — A `DigitizeSyncClient` struct exists with two methods:
  `IngestFile(ctx, filename, reader io.Reader) (jobID string, error)` and
  `DeleteByName(ctx, filename string) error`. `DeleteByName` handles the lookup + delete
  two-step internally.
- **Todo List**
  1. Add `digitize_client.go` in
     `ai-services/internal/pkg/catalog/apiserver/services/connectors/`.
  2. Define `DigitizeSyncClient` with a `*resty.Client` base URL set to the application's
     digitize endpoint. Construct it via `NewDigitizeSyncClient(digitizeURL string)` following the
     TLS-aware pattern in `NewDigitizeBackupClient`.
  3. Implement `IngestFile`: build a multipart POST to `/v1/jobs?operation=ingestion`, attach the
     file bytes as a `files` field using resty's `SetFileReader`, and return the `job_id` from the
     `{"job_id": "..."}` response body.
  4. Implement `DeleteByName`:
     - Call `GET /v1/documents?name={filename}&limit=10` and unmarshal the
       `DocumentsListResponse` (`data[].id`, `data[].name`).
     - Find the entry whose `name` exactly matches the given filename; if none is found, return
       `nil` (already gone — idempotent).
     - Call `DELETE /v1/documents/{doc_id}` and treat a `404` response as success (idempotent).
  5. Return typed errors (HTTP status + body) from both methods so the caller can decide whether
     to mark the connector as `degraded` or log a non-fatal warning.
- **Relevant Context** —
  [`ai-services/internal/pkg/application/common/backup/digitize.go`](ai-services/internal/pkg/application/common/backup/digitize.go),
  [`services/digitize/api/v1/documents.py`](services/digitize/api/v1/documents.py) (delete endpoint, list endpoint),
  [`services/digitize/api/v1/jobs.py`](services/digitize/api/v1/jobs.py) (`POST /v1/jobs` endpoint),
  [`services/digitize/models.py`](services/digitize/models.py) (`DocumentListItem`, `JobCreatedResponse`)
- **Status** — [ ] pending

---

### 2. Implement the three file-change handlers in `ConnectorSyncService`

- **Intent** — Add the logic that processes a `ScanResult` inside the sync loop: ingest new files,
  delete-then-reingest modified files, and delete removed files, using `DigitizeSyncClient`.
- **Expected Outcomes** — After a scan cycle completes, every file in `ToIngest` is submitted to
  digitize, every file in `ToReIngest` has its old document deleted and is re-submitted, and every
  file in `ToRemove` has its document deleted from digitize and its checksum record removed from
  the DB.
- **Todo List**
  1. In `sync.go`, after calling `SSHConnector.Scan()`, instantiate a `DigitizeSyncClient` using
     the digitize endpoint URL resolved from the connector's attached application service record.
  2. **New file** — For each `RemoteFile` in `ScanResult.ToIngest`:
     - Open the file over SFTP (stream bytes, do not buffer the entire file in memory).
     - Call `DigitizeSyncClient.IngestFile(ctx, filepath.Base(f.Path), reader)`.
     - On success, upsert the `ConnectorFileChecksum` record (path, checksum, size,
       `last_seen_at`, `ingested_at = now`).
     - On error, log the failure and record it for `sync_status` aggregation; do **not** abort
       the entire cycle.
  3. **Modified file** — For each `RemoteFile` in `ScanResult.ToReIngest`:
     - Call `DigitizeSyncClient.DeleteByName(ctx, filepath.Base(f.Path))` to remove the old
       document (idempotent if already gone).
     - Open the updated file over SFTP and call `DigitizeSyncClient.IngestFile(...)` with the
       new bytes.
     - On success, upsert the `ConnectorFileChecksum` record with the new checksum and
       `ingested_at = now`.
     - On error, log and continue (partial-error handling, same as new-file case).
  4. **Deleted file** — For each `RemoteFile` in `ScanResult.ToRemove`:
     - Call `DigitizeSyncClient.DeleteByName(ctx, filepath.Base(f.Path))`.
     - On success (or `404`/already-gone), call
       `ConnectorFileChecksumRepository.DeleteChecksum(connectorID, f.Path)` to remove the
       registry entry.
     - On hard error from digitize, log and continue; do not delete the checksum record so the
       file remains in `ToRemove` on the next cycle.
  5. After all three slices are processed, set `connector.sync_status` to `ok` if zero errors
     occurred, or `partial_error` if any file-level errors were recorded.
- **Relevant Context** —
  `ai-services/internal/pkg/catalog/apiserver/services/connectors/sync.go` (new file, sub-task 5 of proposal),
  `ai-services/internal/pkg/catalog/apiserver/services/connectors/sftp_scanner.go` (`ScanResult`, `RemoteFile`),
  `ai-services/internal/pkg/catalog/db/repository` (`ConnectorFileChecksumRepository.UpsertChecksum`, `DeleteChecksum`),
  [`ai-services/internal/pkg/catalog/apiserver/services/sync/sync_service.go`](ai-services/internal/pkg/catalog/apiserver/services/sync/sync_service.go) (error-handling and loop pattern to follow)
- **Status** — [ ] pending

---

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| `doc_id` resolution strategy | Option B — look up by filename at delete time | Avoids storing digitize-internal IDs in the catalog registry; idempotent for already-deleted docs |
| Partial failure handling | Log and continue per-file; set `partial_error` sync status | A single bad file must not abort the entire sync cycle for a connector |
| File streaming | Stream SFTP bytes directly into the HTTP multipart body; no full-file buffer | Avoids OOM on large files |
| Delete idempotency | Treat `404` from `DELETE /v1/documents/{id}` as success | Handles re-runs after partial failures without error noise |
