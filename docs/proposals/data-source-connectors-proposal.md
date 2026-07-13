# Remote connectors implementation plan

## Top-Level Overview

Introduce first-class connector support as a user-facing capability in the catalog product surface so users can create and manage remote data sources from the catalog UI, attach chosen connectors to a digital assistant application during application creation, **and connect or disconnect connectors to a running application at any time without redeploying the pod**.

**Hot-pluggable connector model:**
- Connectors can be attached and detached to a digitize instance **at runtime** via the catalog UI.
- Catalog exposes attach/detach endpoints that, when called against a running application, make a **live callback to the digitize service** to start or stop the corresponding sync worker immediately.
- Digitize stores active connector configs in its own Postgres database (not only in environment variables) so the sync worker can be restarted or reconfigured without a pod restart.
- Digitize exposes a **connector runtime API** (`POST /v1/connectors`, `DELETE /v1/connectors/{id}`, `GET /v1/connectors`) that catalog calls to push or remove connector configuration at runtime.
- Environment variable injection at deploy time is used **only when one or more connectors are already attached at application creation time** — as an optimization that avoids an extra round-trip at first startup.

**Revised implementation direction:**
- Catalog is responsible for connector lifecycle management: creating connectors, storing credentials, generating SSH key pairs, running preflight health checks, managing application-to-connector attachment records, and forwarding connector config to digitize at runtime.
- Catalog calls the digitize service's connector runtime API whenever a connector is attached to or detached from a running application.
- Each digital assistant application deploys its own digitize instance. At deploy time, catalog injects already-attached connector configurations into the digitize pod via the connector runtime API immediately after the pod is healthy — replacing the env-var-at-startup mechanism. Env vars are no longer the primary delivery channel for connector config.
- The digitize service owns all file-system interaction, checksum tracking, change detection, and self-ingestion. It stores connector configs in its own Postgres database and manages a pool of sync workers keyed by connector ID — each worker can be started or stopped independently.
- Connectors are shared catalog resources that multiple applications can attach to.
- Application creation supports attaching one or more existing shared connector resources to the created application instance.
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
| Connector attach/detach to running applications | Catalog (calls digitize runtime API) |
| Connector config push to running digitize instance | Catalog → Digitize connector runtime API |
| Active connector config storage at runtime | Digitize DB (Postgres) |
| Sync worker pool management (start/stop per connector) | Digitize |
| SFTP file system walking | Digitize |
| SHA-256 checksum computation | Digitize |
| Per-file checksum registry | Digitize DB (Postgres) |
| Change detection (new / modified / deleted) | Digitize |
| Ingest pipeline invocation | Digitize (internal) |
| Sync loop / ticker | Digitize (background worker per connector) |

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

User attaches connector to an application (POST /api/v1/applications/{app_id}/connectors)
  → If application is NOT yet deployed:
      → Catalog stores ConnectorAttachment record
      → At deploy time: after digitize pod is healthy, catalog calls
        POST /v1/connectors on the digitize runtime API with connector config
        (decrypted private key PEM, host, port, username, remote_path, sync_interval)
        → Digitize stores connector config in its own DB
        → Digitize starts the sync worker for this connector immediately
  → If application IS already running:
      → Catalog stores ConnectorAttachment record
      → Catalog immediately calls POST /v1/connectors on the digitize runtime API
        with the connector config
        → Digitize stores connector config in its own DB
        → Digitize starts the sync worker for this connector immediately
        → First sync cycle begins within SSH_SYNC_INTERVAL_SECONDS

User detaches connector from an application (DELETE /api/v1/applications/{app_id}/connectors/{connector_id})
  → Catalog removes the ConnectorAttachment record
  → Catalog calls DELETE /v1/connectors/{connector_id} on the digitize runtime API
      → Digitize stops and removes the sync worker for this connector
      → Digitize removes the connector config record from its own DB
      → Digitize removes all checksum records for this connector's remote path
  → UI immediately reflects the disconnected state

Digitize pod starts (with zero connectors pre-attached)
  → settings.py reads general config (DB, embedding, LLM, OpenSearch) from environment
  → ConnectorWorkerManager starts empty (no connectors active)
  → Catalog's deployer calls POST /v1/connectors for each already-attached connector
    after the pod health check passes
  → Each pushed connector starts its own sync worker
```

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Connector config delivery | Catalog pushes config to digitize runtime API after pod is healthy (not env vars) | Enables runtime hot-plug — same code path for initial attach and mid-session attach |
| Worker pool model | One `ConnectorSyncWorker` thread per active connector, keyed by connector ID | Connectors are independent — one connector's failure must not affect another's sync |
| Digitize connector config storage | `active_connectors` table in digitize's own Postgres | Config survives digitize restarts; catalog must re-push only if it initiated a new attach |
| Catalog-to-digitize communication | Catalog resolves the digitize pod's internal endpoint via the deployer's route registry | Same mechanism used to resolve embedding, LLM, and OpenSearch endpoints at deploy time |
| Startup recovery | On digitize restart, load active connectors from DB and restart their workers | Avoids depending on catalog to re-push after a crash; config is self-contained in digitize DB |
| `doc_id` resolution strategy | Look up by filename at delete time — no `doc_id` stored in the registry | Avoids storing digitize-internal IDs in the catalog registry; idempotent for already-deleted docs |
| Partial failure handling | Log and continue per-file; set `partial_error` sync status | A single bad file must not abort the entire sync cycle for a connector |
| File streaming | Stream SFTP bytes directly into the ingest pipeline; no full-file buffer | Avoids OOM on large files |
| Delete idempotency | Treat missing document as success | Handles re-runs after partial failures without error noise |
| Sync logic ownership | Digitize owns all scanning, diffing, ingesting, and deleting | Catalog never calls digitize during sync; only during attach/detach lifecycle events |

---

## Sub-Tasks

### 1. Add connector domain models and persistence `[catalog]`
- **Intent** — Create the persistent catalog-side representation for shared connector resources, auth state, validation state, provider type, and application attachment. The checksum registry and sync state live in digitize, not here. Catalog stores only the connector identity, credentials, and attachment relationships.
- **Expected Outcomes** — New connector data models, repositories, and migrations exist so catalog can store shared connector resources and application-to-connector relationships. No checksum or sync state is stored in catalog.
- **Todo List**
  1. Add a `Connector` model in [`ai-services/internal/pkg/catalog/db/models`](ai-services/internal/pkg/catalog/db/models) following the shape of [`ai-services/internal/pkg/catalog/db/models/service.go`](ai-services/internal/pkg/catalog/db/models/service.go). Fields: `id` (UUID PK), `name`, `type` (enum: `ssh_sftp`; extensible), `status` (enum: `draft`, `auth_required`, `validating`, `ready`, `degraded`, `revoked`), `host`, `username`, `remote_path`, `port` (default 22), `public_key` (text), `encrypted_private_key` (text), `last_validated_at`, `validation_error`, `created_by`, `created_at`, `updated_at`.
  2. Add a `ConnectorAttachment` model in the same package. Fields: `id` (UUID PK), `connector_id` (FK → connectors), `application_id` (FK → applications), `attached_at`, `detached_at` (nullable — set when detached, null when active).
  3. Add SQL migrations in [`ai-services/internal/pkg/catalog/db/migrations/assets`](ai-services/internal/pkg/catalog/db/migrations/assets) for the two tables above (connectors, connector_attachments). Follow the existing numeric prefix naming convention (e.g. `20260430094507_create_connectors_table.sql`).
  4. Add repository interfaces and implementations (`ConnectorRepository`, `ConnectorAttachmentRepository`) in [`ai-services/internal/pkg/catalog/db/repository`](ai-services/internal/pkg/catalog/db/repository) mirroring the pattern in `application_repo.go`. Include: Create, GetByID, List, Update (status, validation fields), Delete, ListByApplication (active attachments only — `detached_at IS NULL`), CreateAttachment, SoftDetachAttachment (sets `detached_at = now()`), DeleteAttachmentsByApplication, DeleteAttachmentsByConnector.
  5. Define a `ConnectorStatus` and `ConnectorType` typed constant block in the models package so all status transitions use named constants rather than raw strings.
  6. Keep connector persistence separate from service and component deployment records — connectors are modeled as shared catalog data-source resources, not deployable catalog services.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/db/models/application.go`](ai-services/internal/pkg/catalog/db/models/application.go), [`ai-services/internal/pkg/catalog/db/models/service.go`](ai-services/internal/pkg/catalog/db/models/service.go), [`ai-services/internal/pkg/catalog/db/migrations/migrations.go`](ai-services/internal/pkg/catalog/db/migrations/migrations.go), [`ai-services/internal/pkg/catalog/db/repository`](ai-services/internal/pkg/catalog/db/repository)
- **Status** — [ ] pending

### 2. Add catalog connector APIs for the UI `[catalog]`
- **Intent** — Expose connector management as a first-class catalog API surface used by the catalog UI, including the SSH/SFTP-specific create flow that returns the generated public key. Also expose attach/detach endpoints on applications so the UI can connect and disconnect connectors to a running application at runtime.
- **Expected Outcomes** — Catalog has authenticated endpoints for creating, listing, updating, deleting, inspecting, and validating SSH/SFTP connectors. The create response includes the generated public key. Application endpoints include attaching and detaching connectors at runtime — both when the application is not yet deployed and when it is already running. The UI can call attach/detach at any time after connector creation.
- **Todo List**
  1. Add `connector_handler.go` in [`ai-services/internal/pkg/catalog/apiserver/handlers`](ai-services/internal/pkg/catalog/apiserver/handlers) with handlers: `CreateConnector`, `ListConnectors`, `GetConnector`, `UpdateConnector`, `DeleteConnector`, `ValidateConnector`. Follow the structure of [`ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go`](ai-services/internal/pkg/catalog/apiserver/handlers/application_handler.go).
  2. Register connector routes in [`ai-services/internal/pkg/catalog/apiserver/router.go`](ai-services/internal/pkg/catalog/apiserver/router.go) under the authenticated `/api/v1` group: `POST /connectors`, `GET /connectors`, `GET /connectors/:id`, `PUT /connectors/:id`, `DELETE /connectors/:id`, `POST /connectors/:id/validate`.
  3. Register application-connector attachment routes under the authenticated `/api/v1` group: `POST /applications/:id/connectors` (attach), `DELETE /applications/:id/connectors/:connector_id` (detach), `GET /applications/:id/connectors` (list active). These are the hot-plug control-plane endpoints.
  4. Define `CreateConnectorRequest` (fields: `name`, `type`, `host`, `port`, `username`, `remote_path`) and `CreateConnectorResponse` (all connector fields plus `public_key` prominently at the top level). `public_key` must be included in every `GetConnector` response as well so the user can retrieve it at any time.
  5. Define `ConnectorResponse` for list and get endpoints including: `id`, `name`, `type`, `status`, `host`, `username`, `remote_path`, `port`, `public_key`, `last_validated_at`, `validation_error`, `applications_count`, `created_at`.
  6. The `ValidateConnector` endpoint must be non-blocking: it enqueues a validation job and immediately returns `202 Accepted` with the current connector status. Status transitions to `validating` immediately, then to `ready` or `degraded` once the background validation completes (reported via the `GetConnector` endpoint).
  7. The `AttachConnector` handler (POST `/applications/:id/connectors`) must: validate the connector is `ready`, persist the `ConnectorAttachment`, check if the target application is currently running, and if so call the `ConnectorPushService` to forward connector config to the live digitize instance. Return `200 OK` with the updated connector attachment record.
  8. The `DetachConnector` handler (DELETE `/applications/:id/connectors/:connector_id`) must: soft-detach the `ConnectorAttachment` (set `detached_at`), check if the target application is currently running, and if so call the `ConnectorPushService` to remove the connector from the live digitize instance. Return `200 OK`.
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

### 4. Implement the connector push service in catalog `[catalog]`
- **Intent** — Add a `ConnectorPushService` in catalog that knows how to resolve the internal endpoint of a running digitize instance and call its connector runtime API. This service is the bridge between catalog's hot-plug control plane and the running digitize pod — it is called whenever a connector is attached to or detached from a running application.
- **Expected Outcomes** — Catalog can push a connector config to or remove a connector from any running digitize instance by making HTTP calls to the digitize pod's internal connector API. Attach and detach flows work identically whether triggered from application creation or from the UI on a running application.
- **Todo List**
  1. Create `ai-services/internal/pkg/catalog/apiserver/services/connectors/push_service.go`. Define the `ConnectorPushService` interface with: `PushConnector(ctx, applicationID string, connector Connector) error` and `RemoveConnector(ctx, applicationID string, connectorID string) error`.
  2. Implement `PushConnector`: resolve the digitize pod's internal base URL for the given application (using the route registry or deployer's known pod-name pattern, consistent with how `EMB_ENDPOINT` and `LLM_ENDPOINT` are resolved in the deployer), decrypt the connector's private key, and `POST /v1/connectors` on the digitize runtime API with the full connector config payload (connector ID, host, port, username, remote_path, decrypted private key PEM, sync_interval_seconds).
  3. Implement `RemoveConnector`: resolve the digitize pod's internal base URL for the given application and `DELETE /v1/connectors/{connector_id}` on the digitize runtime API.
  4. If the digitize pod is not reachable (application not running), `PushConnector` and `RemoveConnector` must return a typed error (`ErrDigitizeNotReachable`) so the caller can decide whether to treat this as a fatal failure or log and continue.
  5. The `AttachConnector` handler in catalog checks the application's runtime status. If the application is `Running`, it calls `ConnectorPushService.PushConnector()` after persisting the attachment record. If the application is not yet running, the attachment is persisted only — the push happens later during the post-deploy hook (sub-task 5).
  6. The `DetachConnector` handler checks the application's runtime status. If the application is `Running`, it calls `ConnectorPushService.RemoveConnector()` after soft-detaching the record. If not running, the soft-detach is persisted only — no digitize call is made.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go), [`ai-services/internal/pkg/catalog/apiserver/services/connectors/`](ai-services/internal/pkg/catalog/apiserver/services/connectors/), [`ai-services/internal/pkg/catalog/apiserver/repository/application_service.go`](ai-services/internal/pkg/catalog/apiserver/repository/application_service.go)
- **Status** — [ ] pending

### 5. Wire connector push into application deployment `[catalog]`
- **Intent** — After a new application is deployed and its digitize pod passes its health check, catalog pushes all already-attached connector configs to the live digitize instance. This ensures that connectors attached before or during application creation are active from first startup without requiring the user to re-attach.
- **Expected Outcomes** — The deployer, after the digitize pod is healthy, loads all active connector attachments for the application and calls `ConnectorPushService.PushConnector()` for each one. The digitize pod starts with all pre-attached connectors active and their sync workers running.
- **Todo List**
  1. In the deployer ([`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go)), after the digitize service is confirmed healthy (post-health-check phase), load all active connector attachments for the application being deployed using `ConnectorAttachmentRepository.ListByApplication()`.
  2. For each active attachment, call `ConnectorPushService.PushConnector()` with the decrypted connector config. Log each push attempt and result. A push failure for one connector must not abort the deployment — log the error and continue.
  3. Remove env-var-based SSH connector injection from the digitize pod template ([`ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl`](ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl)) — `SSH_HOST`, `SSH_PORT`, `SSH_USERNAME`, `SSH_REMOTE_PATH`, `SSH_PRIVATE_KEY_PEM`, and `SSH_SYNC_INTERVAL_SECONDS` are no longer injected as env vars. Connector config is delivered exclusively via the push service API call.
  4. Preserve the current deployment planning flow in [`ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go) — connectors are not deployed as pods and do not participate in component planning.
  5. Expose attached connector information from the application detail API (`GET /api/v1/applications/:id`) as a `connectors` array (active attachments only) so the UI can see which data sources are bound to the application and whether each is actively syncing.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go), [`ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl`](ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl), [`ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go)
- **Status** — [ ] pending

### 6. Add connector runtime API and config schema to the digitize service `[digitize]`
- **Intent** — Expose a connector runtime API inside the digitize FastAPI application so catalog can push or remove connector configurations at any time while the pod is running. Store active connector configs in digitize's own Postgres so they survive pod restarts. This replaces the env-var-at-startup delivery model.
- **Expected Outcomes** — Digitize has `POST /v1/connectors`, `DELETE /v1/connectors/{id}`, and `GET /v1/connectors` endpoints. Connector configs are persisted in an `active_connectors` table. On startup, digitize loads all persisted connector configs and starts their sync workers automatically (restart recovery).
- **Todo List**
  1. Add an `active_connectors` table migration in [`services/digitize/db/scripts/`](services/digitize/db/scripts/). Fields: `id` (text PK — the catalog connector UUID), `type` (text, e.g. `ssh_sftp`), `host` (text), `port` (int), `username` (text), `remote_path` (text), `private_key_pem` (text — decrypted, stored locally for restart recovery), `sync_interval_seconds` (int, default 300), `attached_at` (timestamptz), `last_sync_at` (timestamptz nullable), `sync_status` (text: `idle`, `running`, `partial_error`).
  2. Add CRUD functions for the `active_connectors` table in [`services/digitize/utils/db.py`](services/digitize/utils/db.py): `upsert_active_connector(connector_config)`, `get_active_connector(connector_id)`, `list_active_connectors()`, `delete_active_connector(connector_id)`, `update_connector_sync_status(connector_id, status, last_sync_at)`.
  3. Add `services/digitize/api/v1/connectors.py` with a FastAPI router implementing: `POST /connectors` (receive connector config, upsert to DB, start worker via `ConnectorWorkerManager`), `DELETE /connectors/{connector_id}` (stop worker, delete connector config and all its checksum records from DB), `GET /connectors` (return list of active connectors with sync status). This is an **internal API** — no user authentication required; network-level access control (pod-to-pod only) is sufficient.
  4. Register the new connectors router in [`services/digitize/app.py`](services/digitize/app.py) alongside the existing jobs and documents routers.
  5. During the FastAPI lifespan startup in [`services/digitize/app.py`](services/digitize/app.py), after zombie job recovery, call `list_active_connectors()` from DB and start a `ConnectorSyncWorker` for each returned record via `ConnectorWorkerManager`. This is the restart recovery path.
  6. Remove all SSH_* env var references from [`services/digitize/settings.py`](services/digitize/settings.py) — connector config no longer comes from the environment; it is pushed via the runtime API.
- **Relevant Context** — [`services/digitize/app.py`](services/digitize/app.py), [`services/digitize/api/v1/jobs.py`](services/digitize/api/v1/jobs.py), [`services/digitize/utils/db.py`](services/digitize/utils/db.py), [`services/digitize/db/scripts/`](services/digitize/db/scripts/), [`services/digitize/settings.py`](services/digitize/settings.py)
- **Status** — [ ] pending

### 7. Add the SFTP scanner, sync worker, and worker manager inside digitize `[digitize]`
- **Intent** — Add the runtime file-scanning, change-detection, ingest, and delete logic entirely within the digitize service. A `ConnectorWorkerManager` manages a pool of `ConnectorSyncWorker` threads keyed by connector ID — each can be started or stopped independently. The scanner uses paramiko to walk the remote directory, compute checksums, and diff against the checksum registry. This is the core hot-pluggable runtime engine.
- **Expected Outcomes** — When a connector is pushed to digitize (via the connector runtime API), a sync worker starts immediately for that connector. When a connector is removed, its worker stops immediately. On pod restart, all persisted connectors are recovered and their workers restart automatically. A single connector's failure never affects other connectors' workers.
- **Todo List**
  1. Add `services/digitize/connector/sftp_scanner.py`. Implement `SFTPScanner` using `paramiko`: `connect()` opens an SFTP session using the PEM private key; `scan()` recursively walks `remote_path`, streams each file's bytes through `hashlib.sha256` without buffering the full file in memory, and returns a list of `RemoteFile(path, size, checksum)` objects; `download_file(remote_path)` streams a single file's bytes for staging; `close()` tears down the SFTP and SSH transports cleanly.
  2. Add `services/digitize/connector/sync_worker.py`. Implement `ConnectorSyncWorker` as a background thread per connector:
     - Constructor accepts `connector_id`, `connector_config` (from DB record), and a stop event (`threading.Event`).
     - On each tick: call `SFTPScanner.scan()` to get the current remote file list; load `list_file_checksums(connector_id)` to build the known-state registry; compute the diff: new → ingest, changed → re-ingest, removed → delete.
     - **New file**: download, stage, and call the internal ingest pipeline. On success: `upsert_file_checksum()`. On error: log and continue.
     - **Modified file**: delete old document (treat missing as success), then download, re-ingest, `upsert_file_checksum()`. On error: log and continue.
     - **Deleted file**: delete document (treat missing as success), `delete_file_checksum(remote_path)`. On hard delete error: log and keep checksum record for retry on next cycle.
     - After each full cycle, call `update_connector_sync_status()` with `ok` or `partial_error` and `last_sync_at = now()`.
     - The stop event is checked between file operations — when set, the worker exits its loop cleanly after the current tick completes (no mid-file interruption).
  3. Add `services/digitize/connector/worker_manager.py`. Implement `ConnectorWorkerManager` as a module-level singleton:
     - `start_worker(connector_config)`: create a `threading.Event`, instantiate a `ConnectorSyncWorker`, start it as a daemon thread, and store `{connector_id: (thread, stop_event)}` in an internal dict.
     - `stop_worker(connector_id)`: set the stop event, join the thread with a timeout (e.g. 30 seconds), and remove from the dict.
     - `list_workers()`: return a list of active connector IDs.
     - Thread-safe via a `threading.Lock` on the internal dict.
  4. Update the checksum CRUD functions (sub-task 6) to scope all queries by `connector_id`: `upsert_file_checksum(connector_id, remote_path, checksum, size_bytes)`, `list_file_checksums(connector_id)`, `delete_file_checksum(connector_id, remote_path)`. Add `connector_id` column to the `connector_file_checksums` table migration.
  5. Add `services/digitize/connector/__init__.py` to make `connector` a proper Python sub-package.
  6. Keep scanner, worker, and manager fully encapsulated in the `connector/` sub-package. No connector-specific logic leaks into the job, document, or pipeline modules.
- **Relevant Context** — [`services/digitize/app.py`](services/digitize/app.py), [`services/digitize/utils/storage.py`](services/digitize/utils/storage.py), [`services/digitize/utils/db.py`](services/digitize/utils/db.py), [`services/digitize/pipeline/ingest.py`](services/digitize/pipeline/ingest.py), [`services/digitize/api/v1/jobs.py`](services/digitize/api/v1/jobs.py), [`services/digitize/api/v1/documents.py`](services/digitize/api/v1/documents.py)
- **Status** — [ ] pending

### 8. Wire operational lifecycle and cleanup `[catalog]`
- **Intent** — Ensure shared connectors participate cleanly in status reporting, application visibility, and deletion behavior without being mixed into unrelated deployment primitives. Handle edge cases for the hot-plug lifecycle: deleting a connector that is attached to a running application, and deleting an application that has active connector attachments.
- **Expected Outcomes** — Connector state can be queried and shown in the UI. Detaching or deleting a connector from a running application stops its sync worker in the live digitize pod. Application deletion removes connector attachments and stops workers in the live digitize pod. The shared connector resource itself is never deleted while attached.
- **Todo List**
  1. Add deletion rules in the connector handler: a connector cannot be deleted while one or more active `ConnectorAttachment` records (where `detached_at IS NULL`) reference it. Return `409 Conflict` with a message listing the attached applications. Alternatively expose a `force` flag that calls `DetachConnector` on each attached running application first and then deletes.
  2. Update application deletion handling in [`ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go`](ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go): before tearing down the digitize pod, for each active connector attachment on the application, call `ConnectorPushService.RemoveConnector()` to stop the worker in the live pod. Then soft-detach all `ConnectorAttachment` records for the deleted application without deleting the shared connector itself.
  3. When a connector is hard-deleted from catalog (after no active attachments remain), cascade-delete all its `ConnectorAttachment` history records (both active and soft-detached).
  4. Add connector status reporting fields to the application-level connector list (`GET /api/v1/applications/:id/connectors`) so the UI can show per-connector sync status, last sync time, and source type.
  5. Keep connector cleanup logic separate from component and service deletion logic in [`ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go`](ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go).
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go`](ai-services/internal/pkg/catalog/apiserver/services/deletion/deletion.go), [`ai-services/internal/pkg/catalog/apiserver/repository/application_service.go`](ai-services/internal/pkg/catalog/apiserver/repository/application_service.go)
- **Status** — [ ] pending
