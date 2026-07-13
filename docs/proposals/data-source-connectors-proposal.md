# Remote connectors implementation plan

## Top-Level Overview

Introduce first-class connector support as a user-facing capability in the catalog product surface so users can create and manage remote data sources from the catalog UI and attach chosen connectors to a digital assistant application during application creation.

**Revised implementation direction:**
- Catalog is responsible for connector lifecycle management only: creating connectors, storing credentials, generating SSH key pairs, running preflight health checks, and exposing connector metadata to the UI.
- Catalog does **not** scan files, detect changes, or push content to digitize. Its only runtime responsibility per connector is periodic SFTP health checks.
- Each digital assistant application deploys its own digitize instance. At deploy time, catalog injects the attached connector's configuration (host, username, remote path, port, and decrypted private key PEM) into the digitize pod as environment variables and/or mounted secrets — using the exact same mechanism already used for Postgres and OpenSearch credentials.
- The digitize service owns all file-system interaction, checksum tracking, change detection, and self-ingestion. It reads connector config from its own environment, runs a background sync worker internally, and stores the per-file checksum registry in its own Postgres database.
- Connectors are shared catalog resources that multiple applications can attach to.
- Application creation must support attaching one or more existing shared connector resources to the created application instance.
- The implementation plan focuses on actual service boundaries, API additions, persistence, deployment, and runtime wiring. It does not include a test-planning workstream.

**SSH/SFTP connector scope:** The first remote file system connector type to be fully implemented is an SSH/SFTP connector targeting Unix/Linux remote machines. The system generates an Ed25519 key pair per connector at creation time, stores only the private key (encrypted at rest), displays the public key to the user once so they can add it to their remote server's `authorized_keys`, and subsequently connects over SFTP to enumerate and ingest files. Change detection uses a per-file SHA-256 checksum registry stored in the digitize service's own Postgres database, so only new or modified files are processed by the ingest pipeline. No password or user-supplied private key is ever accepted — catalog-generated public-key authentication is the only supported method for this connector type.

This direction matches the current repo structure where authenticated public APIs already live in [`ai-services/internal/pkg/catalog/apiserver/router.go`](ai-services/internal/pkg/catalog/apiserver/router.go), application creation is centralized in [`ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go`](ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go) and [`ai-services/internal/pkg/catalog/apiserver/repository/application_service.go`](ai-services/internal/pkg/catalog/apiserver/repository/application_service.go), persistence models live in [`ai-services/internal/pkg/catalog/db/models`](ai-services/internal/pkg/catalog/db/models), migrations live in [`ai-services/internal/pkg/catalog/db/migrations/assets`](ai-services/internal/pkg/catalog/db/migrations/assets), and routable deployed services already integrate through [`ai-services/internal/pkg/proxy/caddy.go`](ai-services/internal/pkg/proxy/caddy.go) and [`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go). The `github.com/pkg/sftp` library is already an indirect dependency in [`ai-services/go.mod`](ai-services/go.mod) and `golang.org/x/crypto` is a direct dependency, so SSH key generation and SFTP transport in catalog require no new external dependencies. The digitize service uses `paramiko` (Python SSH library) for SFTP access within its sync worker.

## Service Responsibility Boundary

| Concern | Owner |
|---|---|
| Ed25519 key pair generation | Catalog |
| Encrypted private key storage | Catalog DB |
| Public key display to user | Catalog API |
| SFTP preflight validation (health check) | Catalog |
| Connector CRUD and status management | Catalog |
| Connector config injection at deploy time | Catalog (deployer) |
| SFTP file system walking | Digitize |
| SHA-256 checksum computation | Digitize |
| Per-file checksum registry | Digitize DB (Postgres) |
| Change detection (new / modified / deleted) | Digitize |
| Ingest pipeline invocation | Digitize (internal) |
| Sync loop / ticker | Digitize (background worker) |

## SSH/SFTP Connector — User-Facing Parameters

When a user creates an SSH/SFTP connector the following inputs are involved:

| Field | Direction | Description |
|---|---|---|
| **Public Key** | System → User (display only) | Ed25519 public key generated by catalog at connector creation. User copies this into `~/.ssh/authorized_keys` on the remote host. Never editable or re-submitted by the user. |
| **Host / IP Address** | User → System | The FQDN or IP of the remote server (e.g. `sftp.company.com` or `192.168.1.50`). |
| **Username** | User → System | The Unix account on the remote server the system will authenticate as. |
| **Remote Folder Path** | User → System | The absolute directory path on the remote server containing the files to ingest (e.g. `/var/www/documents/`). |

Port defaults to 22. No password field is exposed.

## SSH/SFTP Connector — Runtime Flow

```
Create Connector (POST /api/v1/connectors)
  → Catalog generates Ed25519 key pair
  → Stores encrypted private key in connector record
  → Returns public key to UI (displayed once, stored in DB for re-display)
  → Connector status: auth-required

User adds public key to remote server authorized_keys
  ↓

Validate Connector (POST /api/v1/connectors/{id}/validate)
  → ConnectorAuthService.ValidateSSH()
      → Dial host:22 with generated private key and provided username
      → Open SFTP session
      → Stat remote folder path
  → On success: connector status → ready
  → On failure: connector status → degraded + validation error stored

Application creation attaches ready connector
  → Catalog stores ConnectorAttachment record
  → At deploy time: deployer decrypts private key and injects connector config
    into the digitize pod (SSH_HOST, SSH_PORT, SSH_USERNAME, SSH_REMOTE_PATH,
    SSH_PRIVATE_KEY_PEM) via env var / mounted secret

Digitize pod starts
  → settings.py reads connector config from environment
  → ConnectorSyncWorker starts as a background thread with a configurable ticker
  → On each tick:
      → SSHSFTPScanner opens SFTP session using injected credentials
      → Recursively walks SSH_REMOTE_PATH
      → For each file: streams content through SHA-256 hasher (no full buffering)
      → Loads per-file checksum registry from digitize's own Postgres
      → Computes diff: new → ingest, changed → re-ingest, removed → delete document
      → For new/changed files: stages file bytes and calls internal ingest pipeline directly
      → For removed files: deletes document via internal pipeline and removes checksum record
      → Upserts checksum registry with updated checksums and timestamps
```

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| `doc_id` resolution strategy | Look up by filename at delete time — no `doc_id` stored in the registry | Avoids storing digitize-internal IDs in the catalog registry; idempotent for already-deleted docs |
| Partial failure handling | Log and continue per-file; set `partial_error` sync status | A single bad file must not abort the entire sync cycle for a connector |
| File streaming | Stream SFTP bytes directly into the ingest pipeline; no full-file buffer | Avoids OOM on large files |
| Delete idempotency | Treat missing document as success | Handles re-runs after partial failures without error noise |
| Sync logic ownership | Digitize owns all scanning, diffing, ingesting, and deleting | Catalog never calls digitize at runtime; no HTTP round-trips from catalog to digitize during sync |

---

## Sub-Tasks

### 1. Add connector domain models and persistence `[catalog]`
- **Intent** — Create the persistent catalog-side representation for shared connector resources, auth state, validation state, provider type, and application attachment. The checksum registry and sync state live in digitize, not here. Catalog stores only the connector identity, credentials, and attachment relationships.
- **Expected Outcomes** — New connector data models, repositories, and migrations exist so catalog can store shared connector resources and application-to-connector relationships. No checksum or sync state is stored in catalog.
- **Todo List**
  1. Add a `Connector` model in [`ai-services/internal/pkg/catalog/db/models`](ai-services/internal/pkg/catalog/db/models) following the shape of [`ai-services/internal/pkg/catalog/db/models/service.go`](ai-services/internal/pkg/catalog/db/models/service.go). Fields: `id` (UUID PK), `name`, `type` (enum: `ssh_sftp`; extensible), `status` (enum: `draft`, `auth_required`, `validating`, `ready`, `degraded`, `revoked`), `host`, `username`, `remote_path`, `port` (default 22), `public_key` (text), `encrypted_private_key` (text), `last_validated_at`, `validation_error`, `created_by`, `created_at`, `updated_at`.
  2. Add a `ConnectorAttachment` model in the same package. Fields: `id` (UUID PK), `connector_id` (FK → connectors), `application_id` (FK → applications), `attached_at`.
  3. Add SQL migrations in [`ai-services/internal/pkg/catalog/db/migrations/assets`](ai-services/internal/pkg/catalog/db/migrations/assets) for the two tables above (connectors, connector_attachments). Follow the existing numeric prefix naming convention (e.g. `20260430094507_create_connectors_table.sql`).
  4. Add repository interfaces and implementations (`ConnectorRepository`, `ConnectorAttachmentRepository`) in [`ai-services/internal/pkg/catalog/db/repository`](ai-services/internal/pkg/catalog/db/repository) mirroring the pattern in `application_repo.go`. Include: Create, GetByID, List, Update (status, validation fields), Delete, ListByApplication (for attachments), DeleteAttachmentsByApplication, DeleteAttachmentsByConnector.
  5. Define a `ConnectorStatus` and `ConnectorType` typed constant block in the models package so all status transitions use named constants rather than raw strings.
  6. Keep connector persistence separate from service and component deployment records — connectors are modeled as shared catalog data-source resources, not deployable catalog services.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/db/models/application.go`](ai-services/internal/pkg/catalog/db/models/application.go), [`ai-services/internal/pkg/catalog/db/models/service.go`](ai-services/internal/pkg/catalog/db/models/service.go), [`ai-services/internal/pkg/catalog/db/migrations/migrations.go`](ai-services/internal/pkg/catalog/db/migrations/migrations.go), [`ai-services/internal/pkg/catalog/db/repository`](ai-services/internal/pkg/catalog/db/repository)
- **Status** — [ ] pending

### 2. Add catalog connector APIs for the UI `[catalog]`
- **Intent** — Expose connector management as a first-class catalog API surface used by the catalog UI, including the SSH/SFTP-specific create flow that returns the generated public key.
- **Expected Outcomes** — Catalog has authenticated endpoints for creating, listing, updating, deleting, inspecting, and validating SSH/SFTP connectors. The create response includes the generated public key so the UI can display it to the user.
- **Todo List**
  1. Add `connector_handler.go` in [`ai-services/internal/pkg/catalog/apiserver/handlers`](ai-services/internal/pkg/catalog/apiserver/handlers) with handlers: `CreateConnector`, `ListConnectors`, `GetConnector`, `UpdateConnector`, `DeleteConnector`, `ValidateConnector`. Follow the structure of [`ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go`](ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go).
  2. Register connector routes in [`ai-services/internal/pkg/catalog/apiserver/router.go`](ai-services/internal/pkg/catalog/apiserver/router.go) under the authenticated `/api/v1` group: `POST /connectors`, `GET /connectors`, `GET /connectors/:id`, `PUT /connectors/:id`, `DELETE /connectors/:id`, `POST /connectors/:id/validate`.
  3. Define `CreateConnectorRequest` (fields: `name`, `type`, `host`, `port`, `username`, `remote_path`) and `CreateConnectorResponse` (all connector fields plus `public_key` prominently at the top level). `public_key` must be included in every `GetConnector` response as well so the user can retrieve it at any time.
  4. Define `ConnectorResponse` for list and get endpoints including: `id`, `name`, `type`, `status`, `host`, `username`, `remote_path`, `port`, `public_key`, `last_validated_at`, `validation_error`, `applications_count`, `created_at`.
  5. The `ValidateConnector` endpoint must be non-blocking: it enqueues a validation job and immediately returns `202 Accepted` with the current connector status. Status transitions to `validating` immediately, then to `ready` or `degraded` once the background validation completes (reported via the `GetConnector` endpoint).
  6. Expose connector details needed by the UI including health, source type, auth method, and count of attached applications. No sync state is reported here — that is owned by the digitize instance.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/router.go`](ai-services/internal/pkg/catalog/apiserver/router.go), [`ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go`](ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go), [`ai-services/internal/pkg/catalog/apiserver/repository/application_service.go`](ai-services/internal/pkg/catalog/apiserver/repository/application_service.go)
- **Status** — [ ] pending

### 3. Implement SSH key generation and connector auth service `[catalog]`
- **Intent** — On connector creation, catalog generates an Ed25519 key pair, persists the encrypted private key, and exposes the public key. On validation, catalog opens a real SFTP connection using the generated key to verify reachability and remote path access. This is the only place catalog ever touches the SSH transport layer.
- **Expected Outcomes** — Every SSH/SFTP connector is provisioned with a system-generated Ed25519 key pair at creation time. The catalog can perform a live SFTP preflight check on demand. No password or user-supplied key material is ever stored or accepted.
- **Todo List**
  1. Create `ai-services/internal/pkg/catalog/apiserver/services/connectors/` as a new package. Add `ssh_keygen.go` with a `GenerateEd25519KeyPair()` function that uses `golang.org/x/crypto/ssh` to produce an Ed25519 private key and return the private key (PEM-encoded) and the public key in OpenSSH authorized-keys format (`ssh.MarshalAuthorizedKey`). Use `crypto/rand` as the source of entropy, consistent with the pattern in [`ai-services/internal/pkg/catalog/apiserver/services/auth/service.go`](ai-services/internal/pkg/catalog/apiserver/services/auth/service.go).
  2. Encrypt the PEM-encoded private key before writing it to the database. Add `EncryptPrivateKey` / `DecryptPrivateKey` to a `crypto.go` file in the same connectors package using AES-GCM with a catalog-configured secret.
  3. Add a `ConnectorAuthService` interface with methods: `ProvisionKeys(ctx) (publicKeyOpenSSH string, encryptedPrivKeyPEM string, error)` and `ValidateSSHConnector(ctx, connector Connector) error`.
  4. Implement `ValidateSSHConnector`: decrypt the stored private key, parse it with `ssh.ParsePrivateKey`, build a `ssh.ClientConfig` using `ssh.PublicKeys` auth method, dial `host:port`, open an SFTP client session using `github.com/pkg/sftp`, call `sftpClient.Stat(remotePath)` to confirm the path exists and is accessible, then close cleanly.
  5. Call `ConnectorAuthService.ProvisionKeys()` inside the `CreateConnector` handler before persisting the record.
  6. Call `ConnectorAuthService.ValidateSSHConnector()` from the background validation job triggered by the `ValidateConnector` API endpoint, updating the connector status to `ready` or `degraded`.
  7. Keep the `ConnectorAuthService` interface provider-agnostic — the SSH/SFTP implementation is one concrete type. Future providers (OAuth, S3 keys) implement the same interface without changing the handler or validation job.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/services/auth/service.go`](ai-services/internal/pkg/catalog/apiserver/services/auth/service.go), [`ai-services/internal/pkg/utils/cert.go`](ai-services/internal/pkg/utils/cert.go), [`ai-services/go.mod`](ai-services/go.mod)
- **Status** — [ ] pending

### 4. Extend application creation to attach connectors and inject credentials at deploy time `[catalog]`
- **Intent** — Make shared connectors selectable and attachable during digital assistant application creation. At deploy time, catalog decrypts the connector's private key and injects all SFTP connection parameters into the digitize pod as environment variables or a mounted secret — using the same mechanism already used for Postgres and OpenSearch credentials.
- **Expected Outcomes** — Application creation persists connector attachments. The deployer injects `SSH_HOST`, `SSH_PORT`, `SSH_USERNAME`, `SSH_REMOTE_PATH`, `SSH_PRIVATE_KEY_PEM`, and `SSH_SYNC_INTERVAL_SECONDS` into the digitize pod at deploy time. Digitize receives a fully formed connector config from its environment without needing to call back to catalog.
- **Todo List**
  1. Extend the create-application request model consumed by [`ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go`](ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go) to include an optional `connector_ids []string` field.
  2. Update [`ai-services/internal/pkg/catalog/apiserver/repository/application_service.go`](ai-services/internal/pkg/catalog/apiserver/repository/application_service.go) so `CreateApplication` validates that each requested connector exists, is visible to the caller, and has `status == ready`. Return a clear validation error if any connector is not ready.
  3. Persist `ConnectorAttachment` records in the same transaction scope as application creation where possible.
  4. In the deployer ([`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go)), after the application and attachment records are created, load the attached connector records for the application being deployed. Decrypt each connector's private key using `ConnectorAuthService.DecryptPrivateKey()` and inject connector configuration into the digitize service's `.Values` or as a mounted secret — following the same pattern used for `digitize-db-secret` and `opensearch-secret` in [`ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl`](ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl).
  5. Update the digitize pod template to declare the SSH connector env vars from the injected values or secret mount.
  6. Preserve the current deployment planning flow in [`ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go) — connectors are not deployed as pods and do not participate in component planning.
  7. Expose attached connector information from the application detail API (`GET /api/v1/applications/:id`) as a `connectors` array so the UI can see which data sources are bound to the application.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go`](ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go), [`ai-services/internal/pkg/catalog/apiserver/repository/application_service.go`](ai-services/internal/pkg/catalog/apiserver/repository/application_service.go), [`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go), [`ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl`](ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl)
- **Status** — [ ] pending

### 5. Add connector settings and checksum schema to the digitize service `[digitize]`
- **Intent** — Extend the digitize service's settings and database schema to support reading SSH connector configuration from environment variables and storing the per-file checksum registry in its own Postgres database. All connector runtime state lives entirely within digitize.
- **Expected Outcomes** — Digitize reads SSH connector config from its environment via `settings.py`. A new `connector_file_checksums` table exists in digitize's Postgres. No connector state is stored in or read from the catalog database at runtime.
- **Todo List**
  1. Add a `ConnectorConfig` settings class in [`services/digitize/settings.py`](services/digitize/settings.py) that reads: `SSH_ENABLED` (bool, default `false`), `SSH_HOST`, `SSH_PORT` (int, default 22), `SSH_USERNAME`, `SSH_REMOTE_PATH`, `SSH_PRIVATE_KEY_PEM` (the decrypted PEM string), `SSH_SYNC_INTERVAL_SECONDS` (int, default 300). Add it as `settings.connector` following the same Pydantic settings pattern used for `settings.digitize` and `settings.database`.
  2. Add a `connector_file_checksums` table migration in [`services/digitize/db/scripts/`](services/digitize/db/scripts/). Fields: `id` (UUID PK), `remote_path` (text, unique), `checksum` (SHA-256 hex, text), `size_bytes` (bigint), `last_seen_at` (timestamptz), `ingested_at` (timestamptz).
  3. Add CRUD functions for the checksum table in [`services/digitize/utils/db.py`](services/digitize/utils/db.py): `upsert_file_checksum(remote_path, checksum, size_bytes)`, `list_file_checksums()` → dict keyed by remote_path, `delete_file_checksum(remote_path)`.
- **Relevant Context** — [`services/digitize/settings.py`](services/digitize/settings.py), [`services/digitize/utils/db.py`](services/digitize/utils/db.py), [`services/digitize/db/scripts/`](services/digitize/db/scripts/)
- **Status** — [ ] pending

### 6. Implement the SFTP scanner and sync worker inside digitize `[digitize]`
- **Intent** — Add the runtime file-scanning, change-detection, ingest, and delete logic entirely within the digitize service. A background worker opens an SFTP session using the injected credentials, walks the remote directory, computes SHA-256 checksums, diffs against the stored registry, and feeds new/changed files directly into the existing internal ingest pipeline. Deleted remote files are removed from the document store via the internal pipeline. No calls to catalog are made at runtime. This sub-task replaces all sync responsibility previously described in the catalog-side ConnectorSyncService (former sub-tasks 8 and 9).
- **Expected Outcomes** — When `SSH_ENABLED=true`, digitize starts a background sync worker on startup. The worker periodically scans the remote path, detects changes via its own checksum registry, and self-ingests new or modified files using the existing internal ingest pipeline. Removed files are deleted from the document store and the checksum registry. Catalog is never called by digitize during sync.
- **Todo List**
  1. Add `services/digitize/connector/sftp_scanner.py`. Implement `SFTPScanner` using `paramiko` for SSH/SFTP: `connect()` opens an SFTP session using the PEM private key from `settings.connector.SSH_PRIVATE_KEY_PEM`; `scan()` recursively walks `settings.connector.SSH_REMOTE_PATH`, streams each file's bytes through `hashlib.sha256` without buffering the full file in memory, and returns a list of `RemoteFile(path, size, checksum)` objects; `download_file(remote_path)` streams a single file's bytes for staging.
  2. Add `services/digitize/connector/sync_worker.py`. Implement `ConnectorSyncWorker` as a background thread:
     - On each tick, call `SFTPScanner.scan()` to get the current remote file list.
     - Load `list_file_checksums()` from DB to build the known-state registry.
     - Compute the diff: files absent in DB → **ingest**, files with a changed checksum → **re-ingest**, files in DB absent from remote → **delete**.
     - **New file**: download via `SFTPScanner.download_file()`, stage to the digitize staging directory, and call the internal ingest pipeline directly (same code path as `POST /v1/jobs` with `operation=ingestion`). On success, call `upsert_file_checksum()`. On error, log and continue — do not abort the cycle.
     - **Modified file**: call the internal document-delete pipeline to remove the old document (treat missing document as success — idempotent), then download and re-ingest as above. On success, call `upsert_file_checksum()` with updated checksum and `ingested_at = now`. On error, log and continue.
     - **Deleted file**: call the internal document-delete pipeline to remove the document (treat missing as success). On success, call `delete_file_checksum(remote_path)` to remove the registry entry. If the delete pipeline returns a hard error, log and continue — keep the checksum record so the file remains in the delete diff on the next cycle.
     - After all three slices are processed, record the sync outcome (`ok` if zero file-level errors, `partial_error` otherwise) in the digitize logs. No sync status is written back to catalog.
  3. The sync interval is read from `settings.connector.SSH_SYNC_INTERVAL_SECONDS`. The worker runs as a daemon thread so it does not block application shutdown.
  4. Start `ConnectorSyncWorker` during the FastAPI app lifespan startup in [`services/digitize/app.py`](services/digitize/app.py) only when `settings.connector.SSH_ENABLED` is `true`. Log clearly when the worker starts and what remote path and interval it uses.
  5. Keep scanner and worker fully encapsulated in the `connector/` sub-package. No connector-specific logic leaks into the job, document, or pipeline modules.
- **Relevant Context** — [`services/digitize/app.py`](services/digitize/app.py), [`services/digitize/utils/storage.py`](services/digitize/utils/storage.py), [`services/digitize/utils/db.py`](services/digitize/utils/db.py), [`services/digitize/pipeline/ingest.py`](services/digitize/pipeline/ingest.py), [`services/digitize/api/v1/jobs.py`](services/digitize/api/v1/jobs.py), [`services/digitize/api/v1/documents.py`](services/digitize/api/v1/documents.py)
- **Status** — [ ] pending

### 7. Wire operational lifecycle and cleanup `[catalog]`
- **Intent** — Ensure shared connectors participate cleanly in status reporting, application visibility, and deletion behavior without being mixed into unrelated deployment primitives.
- **Expected Outcomes** — Connector state can be queried, shown in the UI, and cleaned up safely when detached or deleted.
- **Todo List**
  1. Add deletion rules in the connector handler: a connector cannot be deleted while one or more `ConnectorAttachment` records reference it. Return `409 Conflict` with a message listing the attached applications. Alternatively expose a `force` flag that detaches first and then deletes.
  2. Add application deletion handling in [`ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go`](ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go) so `ConnectorAttachment` records for the deleted application are removed without deleting the shared connector itself.
  3. When a connector is deleted from catalog, cascade-delete all its `ConnectorAttachment` records.
  4. Keep connector cleanup logic separate from component and service deletion logic in [`ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go`](ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go).
  5. Add connector status reporting fields to the UI-facing connector API so the UI can show health, source type, auth status, and count of attached applications.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go`](ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go), [`ai-services/internal/pkg/catalog/apiserver/repository/application_service.go`](ai-services/internal/pkg/catalog/apiserver/repository/application_service.go)
- **Status** — [ ] pending
