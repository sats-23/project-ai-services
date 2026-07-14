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
| Private key at rest (catalog) | AES-256-GCM with HKDF-derived per-connector key | Limits blast radius — one leaked derived key does not expose all connectors |
| Private key at rest (digitize) | Envelope encryption: ciphertext stored alongside DEK encrypted under a per-pod KEK | Plaintext never written to disk; KEK lives only in a Podman secret mount |
| Internal connector API auth | Per-pod random bearer token, mounted as Podman secret, compared with `hmac.compare_digest` | Prevents any process outside the pod pair from calling the internal API |
| Catalog → digitize transport | Self-signed mTLS with certificate pinning; cert generated per-pod at deploy time | Encrypts the channel even on the internal pod network; pins prevent MITM |
| SFTP host key verification | Host key fingerprint captured during preflight validation and pinned in all subsequent connections | Prevents MITM on the SFTP channel between digitize and the remote server |
| KEK delivery | Podman secret mount at `/run/secrets/connector_kek` — never an env var | Secrets are not exposed in `podman inspect` or process env dumps |
| Per-connector HKDF scope | `HKDF(KEK, salt=nil, info="connector-key-v1:<connector_id>")` | Same KEK can safely protect many connectors without key-reuse risk |

---

## Security Architecture

This section describes the three-layer security model that governs all secret material exchanged between the catalog and digitize services. All three layers are required to pass a rigorous security review. All mechanisms work on Podman with no Kubernetes secrets manager, Vault, or external KMS.

### Threat Model

| Threat | Mitigated By |
|---|---|
| Catalog DB breach | AES-256-GCM encryption; plaintext key never stored |
| Digitize DB breach | Envelope encryption; plaintext key never stored |
| Internal pod network interception | mTLS with certificate pinning |
| Rogue process calling connector API | Per-pod bearer token (Podman secret) |
| SFTP channel MITM (remote server impersonation) | Host key fingerprint pinning |
| Single connector compromise exposing all connectors | Per-connector DEK; per-connector HKDF derivation |
| KEK leaking via `podman inspect` or env dump | KEK delivered as Podman secret mount, never as env var |

### Layer 1 — Envelope Encryption (Secrets at Rest)

The Ed25519 private key is never stored in plaintext in any database — neither catalog's nor digitize's.

**Key hierarchy:**

```
catalog_KEK  (Podman secret or operator-supplied env)
    └── HKDF(catalog_KEK, info="connector-key-v1:<connector_id>")
            └── derived_key  →  AES-256-GCM(private_key_pem)  →  catalog DB

digitize_KEK  (Podman secret: /run/secrets/connector_kek, per-pod, per-application)
    └── AES-256-GCM(DEK)  →  encrypted_dek  →  digitize DB
            └── DEK  (unique 32 bytes, generated per connector at push time)
                    └── AES-256-GCM(private_key_pem)  →  private_key_ciphertext  →  digitize DB
```

**Catalog side (at connector creation):**

```
Generate Ed25519 keypair → plaintext_pem (in memory only)
DeriveConnectorKey(catalog_KEK, connector_id) → derived_key
AES-256-GCM encrypt(plaintext_pem, derived_key) → encrypted_pem
Store encrypted_pem in catalog DB
Discard plaintext_pem from memory
```

**Push to digitize (at attach time):**

```
Decrypt encrypted_pem from catalog DB → plaintext_pem (in memory)
Generate fresh DEK = crypto/rand 32 bytes
AES-256-GCM encrypt(plaintext_pem, DEK) → private_key_ciphertext
AES-256-GCM encrypt(DEK, digitize_KEK) → encrypted_dek
Discard plaintext_pem and DEK from memory
Send {private_key_ciphertext, encrypted_dek, ...} to digitize over mTLS
```

**Digitize at rest:**

```
active_connectors table:
  private_key_ciphertext  TEXT   ← AES-256-GCM, keyed by DEK
  encrypted_dek           TEXT   ← AES-256-GCM, keyed by digitize_KEK

No plaintext private key ever touches the digitize DB.
```

**Digitize at sync time (in-memory only):**

```
Decrypt encrypted_dek with digitize_KEK → DEK (in memory)
Decrypt private_key_ciphertext with DEK → plaintext_pem (in memory)
Pass to paramiko; clear variable immediately after session opens
```

**Blast radius analysis:**

| What is compromised | What an attacker can do |
|---|---|
| Catalog DB alone | Nothing — all keys are AES-256-GCM ciphertext |
| Digitize DB alone | Nothing — ciphertext + encrypted DEKs only |
| `catalog_KEK` alone | Nothing — the derived keys are in the DB, not derivable without the DB |
| Digitize DB + `digitize_KEK` | Can decrypt all DEKs for that pod → can decrypt that pod's connector keys only |
| Single connector's DEK | That connector only — HKDF scope means other connectors' derived keys are unrelated |

### Layer 2 — Mutual Authentication (Internal API Security)

The digitize connector runtime API (`POST /v1/connectors`, `DELETE /v1/connectors/{id}`, `GET /v1/connectors`) is secured with a per-pod bearer token.

**Token provisioning:**

```
Deployer generates connector_api_token = crypto/rand 32 bytes → base64url-encoded
Creates Podman secret: podman secret create connector-api-token-<app_id> <token>
Mounts at /run/secrets/connector_api_token inside the digitize pod
Stores encrypted token in catalog DB, scoped to the application
```

**Digitize middleware (Python):**

```python
import hmac
from pathlib import Path

_expected_token: bytes = Path("/run/secrets/connector_api_token").read_bytes().strip()

async def verify_connector_token(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").encode()
    if not hmac.compare_digest(token, _expected_token):
        raise HTTPException(status_code=401, detail="unauthorized")
    # token is never logged
```

**Catalog push service (Go):**

```go
// Token loaded once at startup from catalog DB (decrypted)
req.Header.Set("Authorization", "Bearer "+connectorAPIToken)
```

**Properties:**
- Token is random (not a password or static constant)
- Read from a Podman secret mount — never an env var, never visible in `podman inspect`
- Compared with `hmac.compare_digest` (constant-time — not susceptible to timing attacks)
- Never written to logs on either side
- Rotated on every pod redeploy (new token generated per deployment)

### Layer 3 — Transport Security (mTLS + Certificate Pinning)

All catalog → digitize communication uses HTTPS with a self-signed certificate generated per pod at deploy time. Catalog pins the certificate fingerprint and rejects any connection that presents a different certificate.

**Certificate provisioning at deploy time:**

```
Deployer generates a self-signed Ed25519 TLS certificate for the digitize pod:
  Subject: CN=digitize-<app_id>
  SANs: [digitize-<app_id>, localhost, 127.0.0.1]
  Validity: 10 years (single-operator internal cert; rotated on redeploy)

Creates Podman secrets:
  podman secret create digitize-tls-cert-<app_id> <cert.pem>
  podman secret create digitize-tls-key-<app_id>  <key.pem>

Mounted into pod at:
  /run/secrets/tls/cert.pem  (mode 0444)
  /run/secrets/tls/key.pem   (mode 0400)

Catalog stores SHA-256 fingerprint of the cert (not the cert itself) in the application record.
```

**Podman pod template snippet (`digitize.yaml.tmpl`):**

```yaml
secrets:
  - secret: digitize-tls-cert-{{ .Values.appID }}
    target: /run/secrets/tls/cert.pem
    mode: "0444"
  - secret: digitize-tls-key-{{ .Values.appID }}
    target: /run/secrets/tls/key.pem
    mode: "0400"
  - secret: connector-kek-{{ .Values.appID }}
    target: /run/secrets/connector_kek
    mode: "0400"
  - secret: connector-api-token-{{ .Values.appID }}
    target: /run/secrets/connector_api_token
    mode: "0400"
```

**Digitize Uvicorn startup:**

```bash
uvicorn app:app \
  --ssl-certfile /run/secrets/tls/cert.pem \
  --ssl-keyfile  /run/secrets/tls/key.pem \
  --host 0.0.0.0 --port 4000
```

**Catalog HTTP client (Go) — certificate pinning:**

```go
tlsCfg := &tls.Config{
    InsecureSkipVerify: true, // we do manual fingerprint verification instead of CA chain
    VerifyPeerCertificate: func(rawCerts [][]byte, _ [][]*x509.Certificate) error {
        fingerprint := sha256.Sum256(rawCerts[0])
        if !hmac.Equal(fingerprint[:], storedFingerprint) {
            return fmt.Errorf("digitize TLS certificate fingerprint mismatch: got %x, want %x",
                fingerprint, storedFingerprint)
        }
        return nil
    },
}
httpClient := &http.Client{
    Transport: &http.Transport{TLSClientConfig: tlsCfg},
}
```

**Properties:**
- The private key material (even though now encrypted) never travels over plain HTTP
- Certificate pinning means even a pod-network MITM cannot intercept — they cannot produce the pinned certificate
- `InsecureSkipVerify: true` is intentional here: we are bypassing system CA trust in favour of exact fingerprint matching, which is strictly stronger
- Uses existing `golang.org/x/crypto` — no new dependencies

### Layer 4 (Bonus) — SFTP Host Key Pinning

Without host key pinning, the SFTP connections from digitize to the remote server are vulnerable to MITM. The validation preflight in catalog captures and stores the remote server's host key fingerprint, which is then pinned in all subsequent paramiko connections.

**Catalog validation — capture fingerprint:**

```go
var capturedKey ssh.PublicKey
config := &ssh.ClientConfig{
    User: connector.Username,
    Auth: []ssh.AuthMethod{ssh.PublicKeys(signer)},
    HostKeyCallback: func(hostname string, remote net.Addr, key ssh.PublicKey) error {
        capturedKey = key  // capture on first validation only
        return nil
    },
}
client, err := ssh.Dial("tcp", fmt.Sprintf("%s:%d", connector.Host, connector.Port), config)
// On success: store ssh.FingerprintSHA256(capturedKey) in connector.sftp_host_key_fingerprint
```

**Digitize paramiko — strict pinning:**

```python
class _StrictHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected_fingerprint: str) -> None:
        self._expected = expected_fingerprint

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        actual = key.get_fingerprint().hex()
        if not hmac.compare_digest(actual, self._expected):
            raise paramiko.SSHException(
                f"SFTP host key mismatch for {hostname}: "
                f"expected {self._expected}, got {actual}"
            )

# In SFTPScanner.connect():
ssh_client.set_missing_host_key_policy(_StrictHostKeyPolicy(self.host_key_fingerprint))
```

The `host_key_fingerprint` is included in the connector push payload from catalog and stored in the `active_connectors` table alongside the encrypted key material.

### Complete Security Flow Diagram

```
┌────────────────────────────────────────────────────────────────────────┐
│  CONNECTOR CREATION & VALIDATION                                       │
│                                                                        │
│  User → Catalog API                                                    │
│    POST /connectors {host, username, remote_path}                      │
│      → Generate Ed25519 keypair (crypto/rand)                          │
│      → HKDF(catalog_KEK, "connector-key-v1:<id>") → derived_key       │
│      → AES-256-GCM encrypt(privkey_pem, derived_key) → enc_pem        │
│      → Store enc_pem + public_key in catalog DB                        │
│      → Return public_key to user                                       │
│                                                                        │
│  POST /connectors/:id/validate                                         │
│      → Decrypt enc_pem → privkey_pem (in memory)                      │
│      → SSH dial to remote host (capture host key fingerprint)          │
│      → SFTP Stat(remote_path)                                          │
│      → Store sftp_host_key_fingerprint in catalog DB                  │
│      → status → ready                                                  │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  APPLICATION DEPLOY — SECRET PROVISIONING                              │
│                                                                        │
│  Deployer, before pod starts:                                          │
│    1. Generate connector_api_token = rand(32) → base64url             │
│    2. Generate digitize_KEK = rand(32)                                 │
│    3. Generate TLS cert/key for digitize-<app_id>                      │
│    4. podman secret create connector-api-token-<app_id> <token>       │
│    5. podman secret create connector-kek-<app_id>     <kek>           │
│    6. podman secret create digitize-tls-cert-<app_id> <cert.pem>      │
│    7. podman secret create digitize-tls-key-<app_id>  <key.pem>       │
│    8. Store encrypted(token) + TLS fingerprint in catalog DB           │
│                                                                        │
│  Digitize pod starts:                                                  │
│    → Uvicorn: --ssl-certfile /run/secrets/tls/cert.pem                 │
│               --ssl-keyfile  /run/secrets/tls/key.pem                  │
│    → Middleware reads /run/secrets/connector_api_token                 │
│    → KEK read from /run/secrets/connector_kek                          │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  CONNECTOR ATTACH — SECRETS IN TRANSIT                                 │
│                                                                        │
│  Catalog → Digitize (mTLS, certificate-pinned HTTPS)                  │
│                                                                        │
│  ConnectorPushService.PushConnector():                                 │
│    1. Decrypt enc_pem from catalog DB → privkey_pem (in memory)       │
│    2. Generate fresh DEK = rand(32)                                    │
│    3. AES-256-GCM encrypt(privkey_pem, DEK) → ciphertext              │
│    4. AES-256-GCM encrypt(DEK, digitize_KEK) → encrypted_dek          │
│    5. Clear privkey_pem and DEK from memory                            │
│    6. POST https://digitize-<app_id>:4000/v1/connectors               │
│       Authorization: Bearer <connector_api_token>                      │
│       TLS: pinned to stored cert fingerprint                           │
│       Body: {connector_id, ciphertext, encrypted_dek,                  │
│              host_key_fingerprint, host, port, username,               │
│              remote_path, sync_interval_seconds}                       │
│                                                                        │
│  Digitize receives:                                                    │
│    → HMAC token verify (constant-time)                                 │
│    → Store {ciphertext, encrypted_dek, host_key_fingerprint} in DB    │
│    → Start ConnectorSyncWorker                                         │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  SYNC WORKER — SECRETS IN USE                                          │
│                                                                        │
│  On each sync tick:                                                    │
│    1. Load (ciphertext, encrypted_dek) from digitize DB               │
│    2. Decrypt encrypted_dek with /run/secrets/connector_kek → DEK     │
│    3. Decrypt ciphertext with DEK → privkey_pem (in memory only)      │
│    4. paramiko.SSHClient with _StrictHostKeyPolicy(fingerprint)        │
│    5. Connect, scan, ingest/delete                                     │
│    6. Explicitly clear privkey_pem variable before next iteration      │
│    7. DEK is not retained between ticks                                │
└────────────────────────────────────────────────────────────────────────┘
```

### Security Properties Achieved

| Property | Status | Mechanism |
|---|---|---|
| Private key encrypted at rest — catalog DB | ✅ | AES-256-GCM + per-connector HKDF-derived key |
| Private key encrypted at rest — digitize DB | ✅ | Envelope encryption: DEK + KEK; no plaintext column |
| Per-connector key isolation | ✅ | HKDF scope per connector-id + unique DEK per connector |
| Internal connector API authenticated | ✅ | Per-pod random bearer token via Podman secret mount |
| Constant-time token comparison | ✅ | `hmac.compare_digest` on both Go and Python sides |
| Transport encrypted | ✅ | Self-signed mTLS; Uvicorn `--ssl-certfile/keyfile` |
| Transport identity verified | ✅ | SHA-256 certificate fingerprint pinned in catalog HTTP client |
| SFTP channel MITM prevention | ✅ | Host key fingerprint captured at validation, pinned in paramiko |
| KEK never in env vars or logs | ✅ | Podman secret mount at `/run/secrets/`; read from file |
| Token never in env vars or logs | ✅ | Podman secret mount; `hmac.compare_digest` with no logging |
| Blast radius limited per connector | ✅ | Unique DEK per connector; HKDF scope prevents cross-connector exposure |
| Blast radius limited per application | ✅ | Per-pod KEK means one application's breach doesn't expose other applications |
| No new external dependencies | ✅ | `golang.org/x/crypto` (existing), `cryptography` lib (already a paramiko transitive dep) |
| Works on Podman without Vault / KMS | ✅ | Podman `secret create` + volume mounts; no external secrets manager needed |

---

## Sub-Tasks

### 1. Add connector domain models and persistence `[catalog]`
- **Intent** — Create the persistent catalog-side representation for shared connector resources, auth state, validation state, provider type, and application attachment. The checksum registry and sync state live in digitize, not here. Catalog stores only the connector identity, credentials, and attachment relationships.
- **Expected Outcomes** — New connector data models, repositories, and migrations exist so catalog can store shared connector resources and application-to-connector relationships. No checksum or sync state is stored in catalog.
- **Todo List**
  1. Add a `Connector` model in [`ai-services/internal/pkg/catalog/db/models`](ai-services/internal/pkg/catalog/db/models) following the shape of [`ai-services/internal/pkg/catalog/db/models/service.go`](ai-services/internal/pkg/catalog/db/models/service.go). Fields: `id` (UUID PK), `name`, `type` (enum: `ssh_sftp`; extensible), `status` (enum: `draft`, `auth_required`, `validating`, `ready`, `degraded`, `revoked`), `host`, `username`, `remote_path`, `port` (default 22), `public_key` (text), `encrypted_private_key` (text — AES-256-GCM ciphertext, keyed by a per-connector HKDF-derived key), `sftp_host_key_fingerprint` (text — SHA-256 fingerprint of the remote SFTP server's host key, captured during validation; empty until first successful validation), `last_validated_at`, `validation_error`, `created_by`, `created_at`, `updated_at`.
  2. Add a `ConnectorAttachment` model in the same package. Fields: `id` (UUID PK), `connector_id` (FK → connectors), `application_id` (FK → applications), `attached_at`, `detached_at` (nullable — set when detached, null when active).
  3. Add SQL migrations in [`ai-services/internal/pkg/catalog/db/migrations/assets`](ai-services/internal/pkg/catalog/db/migrations/assets) for the two tables above (connectors, connector_attachments). Follow the existing numeric prefix naming convention (e.g. `20260430094507_create_connectors_table.sql`). The `connectors` migration must include `sftp_host_key_fingerprint TEXT` (nullable, set after first successful validation).
  4. Add repository interfaces and implementations (`ConnectorRepository`, `ConnectorAttachmentRepository`) in [`ai-services/internal/pkg/catalog/db/repository`](ai-services/internal/pkg/catalog/db/repository) mirroring the pattern in `application_repo.go`. Include: Create, GetByID, List, Update (status, validation fields, `sftp_host_key_fingerprint`), Delete, ListByApplication (active attachments only — `detached_at IS NULL`), CreateAttachment, SoftDetachAttachment (sets `detached_at = now()`), DeleteAttachmentsByApplication, DeleteAttachmentsByConnector.
  5. Define a `ConnectorStatus` and `ConnectorType` typed constant block in the models package so all status transitions use named constants rather than raw strings.
  6. Keep connector persistence separate from service and component deployment records — connectors are modeled as shared catalog data-source resources, not deployable catalog services.
  7. **[Security]** The `encrypted_private_key` field stores an AES-256-GCM ciphertext produced by `EncryptConnectorKey(catalog_KEK, connector_id, plaintext_pem)` (see sub-task 3). The field never holds a plaintext PEM value. The `sftp_host_key_fingerprint` field is populated by `ValidateSSHConnector` (see sub-task 3) and is included in the connector push payload so digitize can enforce strict host key pinning.
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
- **Intent** — On connector creation, catalog generates an Ed25519 key pair, persists the encrypted private key (using HKDF-derived AES-256-GCM), and exposes the public key. On validation, catalog opens a real SFTP connection, captures the remote server's host key fingerprint, and stores it for downstream pinning. This is the only place catalog ever touches the SSH transport layer.
- **Expected Outcomes** — Every SSH/SFTP connector is provisioned with a system-generated Ed25519 key pair at creation time. The private key is encrypted with a per-connector derived key — never with a shared static secret. The SFTP host key fingerprint is captured and stored during validation. No password or user-supplied key material is ever stored or accepted.
- **Todo List**
  1. Create `ai-services/internal/pkg/catalog/apiserver/services/connectors/` as a new package. Add `ssh_keygen.go` with a `GenerateEd25519KeyPair()` function that uses `golang.org/x/crypto/ssh` to produce an Ed25519 private key and return the private key (PEM-encoded) and the public key in OpenSSH authorized-keys format (`ssh.MarshalAuthorizedKey`). Use `crypto/rand` as the source of entropy, consistent with the pattern in [`ai-services/internal/pkg/catalog/apiserver/services/auth/service.go`](ai-services/internal/pkg/catalog/apiserver/services/auth/service.go).
  2. **[Security — Envelope Encryption / HKDF]** Add `crypto.go` to the connectors package implementing:
     - `DeriveConnectorKey(kek []byte, connectorID string) []byte` — uses `golang.org/x/crypto/hkdf` with SHA-256, empty salt, and info string `"connector-key-v1:<connectorID>"` to derive a unique 32-byte AES key per connector from the master KEK. This means the same KEK can protect many connectors without key-reuse risk and a single leaked derived key does not expose others.
     - `EncryptConnectorKey(kek []byte, connectorID string, plaintext []byte) (string, error)` — calls `DeriveConnectorKey`, then AES-256-GCM encrypts with a fresh 12-byte random nonce; returns `base64(nonce || ciphertext)`.
     - `DecryptConnectorKey(kek []byte, connectorID string, encoded string) ([]byte, error)` — inverse of the above.
     - The `catalog_KEK` is read from an operator-supplied env var (`CONNECTOR_KEK`) or, preferably, from a mounted file (`/run/secrets/catalog_connector_kek`). It must be exactly 32 bytes (256 bits). The application fails to start if the KEK is absent or malformed.
  3. Add a `ConnectorAuthService` interface with methods: `ProvisionKeys(ctx) (publicKeyOpenSSH string, encryptedPrivKeyPEM string, error)` and `ValidateSSHConnector(ctx, connector Connector) (hostKeyFingerprint string, error)`.
  4. **[Security — Host Key Pinning]** Implement `ValidateSSHConnector`: decrypt the stored private key using `DecryptConnectorKey`, parse it with `ssh.ParsePrivateKey`, build a `ssh.ClientConfig` with a capturing `HostKeyCallback` that records the server's host key on first contact, dial `host:port`, open an SFTP client session using `github.com/pkg/sftp`, call `sftpClient.Stat(remotePath)` to confirm the path exists and is accessible, then close cleanly. Return `ssh.FingerprintSHA256(capturedKey)` as the `hostKeyFingerprint`. The caller persists this value in `connector.sftp_host_key_fingerprint`. Example:
     ```go
     var capturedKey ssh.PublicKey
     config := &ssh.ClientConfig{
         User: connector.Username,
         Auth: []ssh.AuthMethod{ssh.PublicKeys(signer)},
         HostKeyCallback: func(_ string, _ net.Addr, key ssh.PublicKey) error {
             capturedKey = key
             return nil
         },
     }
     // After successful dial:
     return ssh.FingerprintSHA256(capturedKey), nil
     ```
  5. Call `ConnectorAuthService.ProvisionKeys()` inside the `CreateConnector` handler before persisting the record. Use `EncryptConnectorKey` to store the ciphertext — never store the plaintext PEM.
  6. Call `ConnectorAuthService.ValidateSSHConnector()` from the background validation job triggered by the `ValidateConnector` API endpoint. On success, persist the returned `hostKeyFingerprint` in `connector.sftp_host_key_fingerprint` and update status to `ready`. On failure, set status to `degraded`.
  7. Keep the `ConnectorAuthService` interface provider-agnostic — the SSH/SFTP implementation is one concrete type. Future providers (OAuth, S3 keys) implement the same interface without changing the handler or validation job.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/services/auth/service.go`](ai-services/internal/pkg/catalog/apiserver/services/auth/service.go), [`ai-services/internal/pkg/utils/cert.go`](ai-services/internal/pkg/utils/cert.go), [`ai-services/go.mod`](ai-services/go.mod)
- **Status** — [ ] pending

### 4. Implement the connector push service in catalog `[catalog]`
- **Intent** — Add a `ConnectorPushService` in catalog that knows how to resolve the internal endpoint of a running digitize instance and call its connector runtime API over mTLS with certificate pinning and a bearer token. The private key is never sent in plaintext — it is re-encrypted under a per-connector DEK before transit. This service is the bridge between catalog's hot-plug control plane and the running digitize pod.
- **Expected Outcomes** — Catalog can push a connector config to or remove a connector from any running digitize instance over a mutually authenticated, certificate-pinned HTTPS connection. The push payload contains only ciphertext — no plaintext key material ever travels over the wire. Attach and detach flows work identically whether triggered from application creation or from the UI on a running application.
- **Todo List**
  1. Create `ai-services/internal/pkg/catalog/apiserver/services/connectors/push_service.go`. Define the `ConnectorPushService` interface with: `PushConnector(ctx, applicationID string, connector Connector) error` and `RemoveConnector(ctx, applicationID string, connectorID string) error`.
  2. **[Security — Envelope Encryption at Push Time]** Implement `PushConnector`:
     - Resolve the digitize pod's internal base HTTPS URL for the given application (same pattern as `EMB_ENDPOINT`/`LLM_ENDPOINT` resolution in the deployer, but using `https://`).
     - Decrypt the connector's `encrypted_private_key` from catalog DB using `DecryptConnectorKey(catalog_KEK, connector_id)` → `plaintext_pem` (in memory only).
     - Retrieve the application's `digitize_KEK` (stored encrypted in catalog DB, scoped to the application).
     - Generate a fresh `DEK = crypto/rand 32 bytes`.
     - `AES-256-GCM encrypt(plaintext_pem, DEK)` → `private_key_ciphertext`.
     - `AES-256-GCM encrypt(DEK, digitize_KEK)` → `encrypted_dek`.
     - Explicitly zero `plaintext_pem` and `DEK` in memory before the HTTP call.
     - `POST https://digitize-<app_id>:4000/v1/connectors` with the `ConnectorPushPayload`:
       ```go
       type ConnectorPushPayload struct {
           ConnectorID          string `json:"connector_id"`
           Type                 string `json:"type"`
           Host                 string `json:"host"`
           Port                 int    `json:"port"`
           Username             string `json:"username"`
           RemotePath           string `json:"remote_path"`
           PrivateKeyCiphertext string `json:"private_key_ciphertext"` // base64(nonce||ciphertext), keyed by DEK
           EncryptedDEK         string `json:"encrypted_dek"`          // base64(nonce||ciphertext), keyed by digitize KEK
           HostKeyFingerprint   string `json:"host_key_fingerprint"`   // SHA-256 fingerprint from validation
           SyncIntervalSeconds  int    `json:"sync_interval_seconds"`
       }
       ```
  3. **[Security — mTLS + Certificate Pinning]** Build the HTTP client used by `PushConnector` and `RemoveConnector` with certificate pinning:
     - Retrieve the application's stored `digitize_tls_fingerprint` (SHA-256 of the self-signed TLS cert, stored in catalog DB at deploy time).
     - Construct a `*tls.Config` with `InsecureSkipVerify: true` and a `VerifyPeerCertificate` callback that computes `sha256.Sum256(rawCerts[0])` and rejects the connection with an error if the fingerprint does not match the stored value (using `hmac.Equal` for constant-time comparison).
     - Set `Authorization: Bearer <connector_api_token>` on every request (token loaded from catalog DB, decrypted, scoped to the application).
  4. Implement `RemoveConnector`: resolve the digitize pod's base HTTPS URL and `DELETE https://digitize-<app_id>:4000/v1/connectors/{connector_id}` using the same pinned HTTP client and bearer token. No key material is involved in a remove call.
  5. If the digitize pod is not reachable (application not running), `PushConnector` and `RemoveConnector` must return a typed error (`ErrDigitizeNotReachable`) so the caller can decide whether to treat this as a fatal failure or log and continue.
  6. The `AttachConnector` handler in catalog checks the application's runtime status. If the application is `Running`, it calls `ConnectorPushService.PushConnector()` after persisting the attachment record. If the application is not yet running, the attachment is persisted only — the push happens later during the post-deploy hook (sub-task 5).
  7. The `DetachConnector` handler checks the application's runtime status. If the application is `Running`, it calls `ConnectorPushService.RemoveConnector()` after soft-detaching the record. If not running, the soft-detach is persisted only — no digitize call is made.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go), [`ai-services/internal/pkg/catalog/apiserver/services/connectors/`](ai-services/internal/pkg/catalog/apiserver/services/connectors/), [`ai-services/internal/pkg/catalog/apiserver/repository/application_service.go`](ai-services/internal/pkg/catalog/apiserver/repository/application_service.go)
- **Status** — [ ] pending

### 5. Wire connector push into application deployment `[catalog]`
- **Intent** — Before the digitize pod starts, the deployer provisions all Podman secrets required for secure connector operation (KEK, API token, TLS cert/key). After the pod passes its health check, catalog pushes all already-attached connector configs using the secure envelope-encrypted push service. This ensures that connectors attached before or during application creation are active from first startup.
- **Expected Outcomes** — The deployer generates and mounts all per-application secrets before pod start. The digitize pod starts with all pre-attached connectors active, their sync workers running, and all key material encrypted at rest. No plaintext private key is ever present in the pod template, env vars, or logs.
- **Todo List**
  1. **[Security — Per-Pod Secret Provisioning]** In the deployer ([`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go)), before rendering the pod template, generate and create the following Podman secrets for each new digitize pod:
     - `connector_api_token` — `crypto/rand` 32 bytes → base64url; created as `podman secret create connector-api-token-<app_id>`.
     - `digitize_KEK` — `crypto/rand` 32 bytes; created as `podman secret create connector-kek-<app_id>`.
     - Self-signed Ed25519 TLS certificate/key pair for the digitize instance — created as `podman secret create digitize-tls-cert-<app_id>` and `podman secret create digitize-tls-key-<app_id>`. Use the existing TLS utilities in [`ai-services/internal/pkg/utils/cert.go`](ai-services/internal/pkg/utils/cert.go) for certificate generation.
     - Store the encrypted `connector_api_token` and the SHA-256 fingerprint of the TLS certificate in the application record in catalog DB (both are needed by `ConnectorPushService` when calling the running pod).
     - Store the `digitize_KEK` encrypted under the `catalog_KEK` in the application record in catalog DB (needed by `ConnectorPushService` to produce `encrypted_dek` payloads).
  2. After the digitize service is confirmed healthy (post-health-check phase), load all active connector attachments for the application being deployed using `ConnectorAttachmentRepository.ListByApplication()`.
  3. For each active attachment, call `ConnectorPushService.PushConnector()` (which uses mTLS + bearer token + envelope encryption as described in sub-task 4). Log each push attempt and result. A push failure for one connector must not abort the deployment — log the error and continue.
  4. Remove env-var-based SSH connector injection from the digitize pod template ([`ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl`](ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl)) — `SSH_HOST`, `SSH_PORT`, `SSH_USERNAME`, `SSH_REMOTE_PATH`, `SSH_PRIVATE_KEY_PEM`, and `SSH_SYNC_INTERVAL_SECONDS` are no longer injected as env vars. Connector config is delivered exclusively via the secure push service API call.
  5. Add the four new Podman secret mounts to the digitize pod template:
     ```yaml
     secrets:
       - secret: connector-api-token-{{ .Values.appID }}
         target: /run/secrets/connector_api_token
         mode: "0400"
       - secret: connector-kek-{{ .Values.appID }}
         target: /run/secrets/connector_kek
         mode: "0400"
       - secret: digitize-tls-cert-{{ .Values.appID }}
         target: /run/secrets/tls/cert.pem
         mode: "0444"
       - secret: digitize-tls-key-{{ .Values.appID }}
         target: /run/secrets/tls/key.pem
         mode: "0400"
     ```
  6. Preserve the current deployment planning flow in [`ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go) — connectors are not deployed as pods and do not participate in component planning.
  7. Expose attached connector information from the application detail API (`GET /api/v1/applications/:id`) as a `connectors` array (active attachments only) so the UI can see which data sources are bound to the application and whether each is actively syncing.
- **Relevant Context** — [`ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/repository/podman/deployer.go), [`ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl`](ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl), [`ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go`](ai-services/internal/pkg/catalog/apiserver/services/deployment/planner.go)
- **Status** — [ ] pending

### 6. Add connector runtime API and config schema to the digitize service `[digitize]`
- **Intent** — Expose a connector runtime API inside the digitize FastAPI application so catalog can push or remove connector configurations at any time while the pod is running. The API is protected by a per-pod bearer token and listens over mTLS. Connector configs are stored envelope-encrypted in digitize's own Postgres so they survive pod restarts without ever touching plaintext.
- **Expected Outcomes** — Digitize has `POST /v1/connectors`, `DELETE /v1/connectors/{id}`, and `GET /v1/connectors` endpoints, all protected by a bearer token middleware and served over mTLS. Connector configs are persisted with `private_key_ciphertext` and `encrypted_dek` columns — no plaintext key column. On startup, digitize loads all persisted connector configs and starts their sync workers automatically (restart recovery — decryption happens in-worker at sync time).
- **Todo List**
  1. **[Security — Encrypted Schema]** Add an `active_connectors` table migration in [`services/digitize/db/scripts/`](services/digitize/db/scripts/). Fields: `id` (text PK — the catalog connector UUID), `type` (text, e.g. `ssh_sftp`), `host` (text), `port` (int), `username` (text), `remote_path` (text), `private_key_ciphertext` (text — AES-256-GCM ciphertext of the Ed25519 private key PEM, keyed by connector-specific DEK; **no plaintext key column**), `encrypted_dek` (text — AES-256-GCM ciphertext of the DEK, keyed by the pod's `digitize_KEK`), `host_key_fingerprint` (text — SHA-256 fingerprint of the remote SFTP server's host key, used for strict pinning), `sync_interval_seconds` (int, default 300), `attached_at` (timestamptz), `last_sync_at` (timestamptz nullable), `sync_status` (text: `idle`, `running`, `partial_error`).
  2. Add CRUD functions for the `active_connectors` table in [`services/digitize/utils/db.py`](services/digitize/utils/db.py): `upsert_active_connector(connector_config)`, `get_active_connector(connector_id)`, `list_active_connectors()`, `delete_active_connector(connector_id)`, `update_connector_sync_status(connector_id, status, last_sync_at)`. These functions work with the ciphertext columns — they never decrypt or re-encrypt.
  3. **[Security — Bearer Token Middleware]** Add `services/digitize/api/v1/connectors.py` with a FastAPI router. Before registering any routes, define a dependency:
     ```python
     import hmac
     from pathlib import Path
     from fastapi import Depends, HTTPException, Request

     _TOKEN = Path("/run/secrets/connector_api_token").read_bytes().strip()

     async def _require_connector_token(request: Request) -> None:
         raw = request.headers.get("Authorization", "").removeprefix("Bearer ").encode()
         if not hmac.compare_digest(raw, _TOKEN):
             raise HTTPException(status_code=401, detail="unauthorized")
         # do not log the token or the raw header value
     ```
     Apply this dependency to all three routes: `POST /connectors`, `DELETE /connectors/{connector_id}`, `GET /connectors`. The `POST` endpoint receives a `ConnectorPushPayload` (matching the Go struct in sub-task 4), upserts the encrypted fields to DB, and starts the sync worker. The `DELETE` endpoint stops the worker and deletes the connector config and all its checksum records. The `GET` endpoint returns active connectors with their sync status (excluding all key material fields).
  4. Register the new connectors router in [`services/digitize/app.py`](services/digitize/app.py) alongside the existing jobs and documents routers.
  5. **[Security — mTLS Listener]** Update Uvicorn startup to use the mounted TLS certificate and key:
     ```bash
     uvicorn app:app \
       --ssl-certfile /run/secrets/tls/cert.pem \
       --ssl-keyfile  /run/secrets/tls/key.pem \
       --host 0.0.0.0 --port 4000
     ```
     This is configured in the pod's startup command in [`services/digitize/podman/templates/digitize.yaml.tmpl`](ai-services/assets/services/digitize/podman/templates/digitize.yaml.tmpl). Uvicorn uses the mounted Podman secrets directly — no cert file is written to the image or the container filesystem.
  6. During the FastAPI lifespan startup in [`services/digitize/app.py`](services/digitize/app.py), after zombie job recovery, call `list_active_connectors()` from DB and start a `ConnectorSyncWorker` for each returned record via `ConnectorWorkerManager`. This is the restart recovery path. Workers receive the ciphertext config from DB and decrypt in-worker at sync time (see sub-task 7).
  7. Remove all SSH_* env var references from [`services/digitize/settings.py`](services/digitize/settings.py) — connector config no longer comes from the environment; it is pushed via the authenticated runtime API.
- **Relevant Context** — [`services/digitize/app.py`](services/digitize/app.py), [`services/digitize/api/v1/jobs.py`](services/digitize/api/v1/jobs.py), [`services/digitize/utils/db.py`](services/digitize/utils/db.py), [`services/digitize/db/scripts/`](services/digitize/db/scripts/), [`services/digitize/settings.py`](services/digitize/settings.py)
- **Status** — [ ] pending

### 7. Add the SFTP scanner, sync worker, and worker manager inside digitize `[digitize]`
- **Intent** — Add the runtime file-scanning, change-detection, ingest, and delete logic entirely within the digitize service. A `ConnectorWorkerManager` manages a pool of `ConnectorSyncWorker` threads keyed by connector ID. The scanner decrypts the connector's private key in-memory per sync tick and uses strict host key pinning. Key material is zeroized after use.
- **Expected Outcomes** — When a connector is pushed to digitize (via the authenticated connector runtime API), a sync worker starts immediately for that connector. Each sync tick decrypts the private key in-memory, connects with strict SFTP host key pinning, and clears the key from memory before the tick ends. The plaintext key is never held in a long-lived variable or written to disk. A single connector's failure never affects other connectors' workers.
- **Todo List**
  1. **[Security — In-Memory Decryption + Host Key Pinning]** Add `services/digitize/connector/sftp_scanner.py`. Implement `SFTPScanner` using `paramiko`:
     - Constructor accepts `connector_config` (the DB record, including `private_key_ciphertext`, `encrypted_dek`, and `host_key_fingerprint`).
     - `connect()`: load `digitize_KEK` from `/run/secrets/connector_kek`; decrypt `encrypted_dek` with the KEK to get the DEK; decrypt `private_key_ciphertext` with the DEK to get `privkey_pem` (all in memory). Load the key via `paramiko.Ed25519Key.from_private_key(io.StringIO(privkey_pem))`. Overwrite `privkey_pem` string immediately after loading. Set the host key policy using `_StrictHostKeyPolicy(host_key_fingerprint)` (see below) — **never** use `paramiko.AutoAddPolicy`. Open the SFTP session.
     - `scan()`: recursively walks `remote_path`, streams each file's bytes through `hashlib.sha256` without buffering the full file in memory, and returns a list of `RemoteFile(path, size, checksum)` objects.
     - `download_file(remote_path)`: streams a single file's bytes for staging.
     - `close()`: tears down the SFTP and SSH transports cleanly.
     - **[Security — Strict Host Key Policy]** Define `_StrictHostKeyPolicy` in the same file:
       ```python
       import hmac
       import paramiko

       class _StrictHostKeyPolicy(paramiko.MissingHostKeyPolicy):
           def __init__(self, expected_fingerprint: str) -> None:
               self._expected = expected_fingerprint

           def missing_host_key(
               self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
           ) -> None:
               actual = key.get_fingerprint().hex()
               if not hmac.compare_digest(actual, self._expected):
                   raise paramiko.SSHException(
                       f"SFTP host key mismatch for {hostname}: "
                       f"expected {self._expected}, got {actual}"
                   )
       ```
       This replaces `paramiko.AutoAddPolicy` entirely — no connection is ever accepted without a matching fingerprint.
  2. Add `services/digitize/connector/sync_worker.py`. Implement `ConnectorSyncWorker` as a background thread per connector:
     - Constructor accepts `connector_id`, `connector_config` (the DB record with ciphertext fields), and a stop event (`threading.Event`).
     - On each tick: instantiate `SFTPScanner(connector_config)`, call `scanner.connect()` (decrypts key in-memory), call `scanner.scan()` to get the current remote file list, then `scanner.close()`. The decrypted key exists only within the `connect()`→`close()` window.
     - Load `list_file_checksums(connector_id)` to build the known-state registry; compute the diff: new → ingest, changed → re-ingest, removed → delete.
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
