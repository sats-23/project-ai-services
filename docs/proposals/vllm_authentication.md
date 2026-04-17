# Design Proposal: vLLM API Authentication

---

## 1. Executive Summary

**vLLM Authentication** provides secure, API key-based authentication for all vLLM inference services (instruct, embedding, and reranker models). By implementing automatic API key generation during application creation with an opt-out mechanism, the system ensures that AI services are secured by default while maintaining flexibility for development and testing scenarios.

## 2. Problem Statement

### Current State
- vLLM services (instruct, embedding, reranker) are deployed **without authentication**
- Any client with network access can consume vLLM APIs
- No access control or audit trail for API usage
- Security risk in production environments

### Requirements
1. **Secure by Default**: Authentication must be enabled automatically during application creation
2. **Automatic Key Generation**: API keys should be generated without manual intervention
3. **Opt-Out Mechanism**: Users can disable authentication via explicit flag for development/testing
4. **Backward Compatible**: Existing deployments should continue to work
5. **Minimal Overhead**: No performance degradation or complex configuration

## 3. Solution Architecture

### 3.1 Authentication Flow

```
Client Service
      |
      | HTTP Request + Authorization: Bearer <api-key>
      v
vLLM Server
      |
      +--> API Key Validation
            |
            +--[Valid Key]-------> Model Inference --> Response
            |
            +--[Invalid/Missing]--> 401 Unauthorized
```

### 3.2 System Components

| Component | Role | Implementation |
|-----------|------|----------------|
| **vLLM Server** | Validates API keys using `--api-key` parameter | Native vLLM support (v0.4.1+) |
| **Client Services** | Include `Authorization: Bearer <key>` header | Python utilities (misc_utils, emb_utils, llm_utils) |
| **Key Generator** | Creates cryptographically secure random keys | Go crypto/rand (32 bytes, base64 encoded) |
| **Configuration** | Controls auth behavior and stores keys | values.yaml with vllm.authEnabled flag |

## 4. Feature Specification

### 4.1 Default Behavior (Authentication Enabled)

When a user creates an application **without** specifying authentication preferences:

```bash
$ ai-services application create my-app -t rag

Generating vLLM API keys...
✓ Generated API key for vLLM instruct service
✓ Generated API key for vLLM embedding service  
✓ Generated API key for vLLM reranker service

=== vLLM API Keys (Generated) ===
IMPORTANT: Save these keys securely.

Instruct API Key:  a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6
Embedding API Key: b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7
Reranker API Key:  c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v2w3x4y5z6a7b8

Keys saved in: /var/lib/ai-services/applications/my-app/generated-values.yaml
=====================================
```

**What Happens:**
1. CLI generates three unique API keys (one per vLLM service)
2. **Keys are populated back into the values file** (e.g., `/var/lib/ai-services/applications/my-app/values.yaml`)
3. Keys are displayed once to the user for reference
4. Application is deployed using the updated values file
5. vLLM servers start with `--api-key` parameter (from values)
6. Client services automatically use keys via environment variables (from values)

**Key Storage - Hybrid Approach** (Similar to OpenSearch auth password):

| Environment | Storage Method | How Keys Are Used |
|-------------|---------------|-------------------|
| **Podman** | Values file directly | Keys read from values, passed as env vars to containers |
| **OpenShift** | Kubernetes Secret (created from values) | Keys stored in Secret, mounted/referenced in deployments |

**Key Generation & Deployment Flow**:
```
1. Generate Keys (if authEnabled=true and keys empty)
2. Populate Keys into Values Structure
3. Save Values File to Disk
4. Deploy Application:
   
   Podman:
   ├─> Read keys from values file
   └─> Pass as environment variables to containers
   
   OpenShift:
   ├─> Create vllm-api-keys Secret from values
   ├─> Reference Secret in InferenceServices (vLLM pods)
   └─> Reference Secret in Deployments (client pods)
```

### 4.2 Disabling Authentication (Opt-Out)

For development or testing scenarios, users can disable authentication via the `--params` flag:

```bash
$ ai-services application create my-app -t rag --params vllm.authEnabled=false

⚠ vLLM authentication is disabled (not recommended for production)
✓ Application 'my-app' created successfully
```

**What Happens:**
1. `vllm.authEnabled` is set to `false` in values
2. No API keys are generated
3. vLLM servers start without `--api-key` parameter
4. Client services do not include Authorization headers

### 4.3 Using Custom API Keys

Users can provide their own API keys:

```bash
$ ai-services application create my-app -t rag \
    --params instruct.apiKey=custom-key-1,embedding.apiKey=custom-key-2
```

## 5. Configuration Structure

### 5.1 values.yaml Schema

```yaml
vllm:
  authEnabled: true  # Default: true (authentication enabled)

instruct:
  apiKey: ""  # Auto-generated if authEnabled=true and empty

embedding:
  apiKey: ""  # Auto-generated if authEnabled=true and empty

reranker:
  apiKey: ""  # Auto-generated if authEnabled=true and empty
```

### 5.2 Configuration Logic

```
IF vllm.authEnabled == true:
    IF apiKey is empty:
        Generate new API key
    ELSE:
        Use provided API key
    Pass API key to vLLM server and clients
ELSE:
    Do not use authentication
```

## 6. Implementation Details

### 6.1 Server-Side (vLLM)

#### Podman Implementation

vLLM servers read keys from values file and pass via environment variables:

```yaml
# vllm-server.yaml.tmpl
{{- if and .Values.vllm.authEnabled .Values.instruct.apiKey }}
environment:
  - VLLM_INSTRUCT_API_KEY={{ .Values.instruct.apiKey }}
command:
  - --api-key ${VLLM_INSTRUCT_API_KEY}
{{- end }}
```

#### OpenShift Implementation

**Step 1: Create Kubernetes Secret** (similar to opensearch-credentials-secret.yaml)

```yaml
# vllm-api-keys-secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: "vllm-api-keys"
  labels:
    ai-services.io/application: {{ .Release.Name }}
    ai-services.io/template: {{ .Chart.Name }}
type: Opaque
stringData:
  instruct-api-key: {{ .Values.instruct.apiKey | quote }}
  embedding-api-key: {{ .Values.embedding.apiKey | quote }}
  reranker-api-key: {{ .Values.reranker.apiKey | quote }}
```

**Step 2: Reference Secret in InferenceServices**

```yaml
# instruct-inferenceservice.yaml
spec:
  predictor:
    model:
      env:
      - name: VLLM_INSTRUCT_API_KEY
        valueFrom:
          secretKeyRef:
            name: vllm-api-keys
            key: instruct-api-key
      args:
      - --api-key=${VLLM_INSTRUCT_API_KEY}
```

**Step 3: Reference Secret in Client Deployments**

```yaml
# backend-deployment.yaml
spec:
  template:
    spec:
      containers:
      - name: server
        env:
        - name: VLLM_INSTRUCT_API_KEY
          valueFrom:
            secretKeyRef:
              name: vllm-api-keys
              key: instruct-api-key
```

#### Behavior Matrix

| authEnabled | apiKey | vLLM Behavior |
|-------------|--------|---------------|
| true | present | Authentication enabled with provided key |
| true | empty | Authentication enabled with auto-generated key |
| false | present | Authentication disabled (key ignored) |
| false | empty | Authentication disabled |

### 6.2 Client-Side (Python Services)

All Python services load API keys from environment variables and include Authorization headers when keys are present:

```python
# Load keys at session creation
VLLM_INSTRUCT_API_KEY = os.getenv("VLLM_INSTRUCT_API_KEY")
VLLM_EMBEDDING_API_KEY = os.getenv("VLLM_EMBEDDING_API_KEY")
VLLM_RERANKER_API_KEY = os.getenv("VLLM_RERANKER_API_KEY")

# Add to headers when making API calls
if VLLM_EMBEDDING_API_KEY:
    headers["Authorization"] = f"Bearer {VLLM_EMBEDDING_API_KEY}"
```

### 6.3 CLI Implementation (Go) 

#### Key Generation
```go
func GenerateSecureApiKey() (string, error) {
    bytes := make([]byte, 32)  // 256 bits
    rand.Read(bytes)
    return base64.StdEncoding.EncodeToString(bytes), nil
}
```

#### Integration Points
1. Implement key generation in application creation flow
2. Check `vllm.authEnabled` value (default: true)
3. If `authEnabled=true` and API keys are empty, generate them
4. Populate generated keys back into the values structure
5. Display generated keys to user
6. Save updated values to file
7. Create secrets (OpenShift specific)
8. Deploy application using the updated values file

**Implementation Flow**:
```go
// Pseudo-code for create.go
func Create(appName, template string, params map[string]string) error {
    // 1. Load and merge values
    values := loadTemplateValues(template)
    mergeParams(values, params)
    
    // 2. Check if auth is enabled and keys are empty
    if values.vllm.authEnabled && values.instruct.apiKey == "" {
        // 3. Generate keys
        values.instruct.apiKey = GenerateSecureApiKey()
        values.embedding.apiKey = GenerateSecureApiKey()
        values.reranker.apiKey = GenerateSecureApiKey()
        
        // 4. Display keys to user
        fmt.Println("=== vLLM API Keys (Generated) ===")
        fmt.Printf("Instruct:  %s\n", values.instruct.apiKey)
        fmt.Printf("Embedding: %s\n", values.embedding.apiKey)
        fmt.Printf("Reranker:  %s\n", values.reranker.apiKey)
    }
    
    // 5. Save values file with keys
    saveValuesFile(appDir, values)
    
    // 6. Deploy using saved values
    deployApplication(appName, template, valuesFile)
}
```

## 7. Security Considerations

### 7.1 Key Generation
- **Entropy**: 32 bytes (256 bits) of cryptographically secure random data
- **Encoding**: Base64 for easy handling (44 characters)
- **Uniqueness**: Each service gets a unique key
- **Source**: Go's `crypto/rand` package (CSPRNG)

### 7.2 Key Storage
- **Format**: YAML file with clear structure, Secrets for OpenShift
- **Display**: Keys shown only once during creation

## 8. Testing Strategy

### 8.1 Test Scenarios

**Scenario 1: Default Behavior (Auth Enabled)**
- Create application without flags
- Verify keys are generated and displayed
- Verify vLLM servers start with authentication
- Test API calls succeed with keys, fail without (401)

**Scenario 2: Disabled Authentication**
- Create application with `--params vllm.authEnabled=false`
- Verify no keys are generated
- Verify vLLM servers start without authentication
- Test API calls succeed without keys

**Scenario 3: Custom Keys**
- Create application with custom keys via `--params`
- Verify custom keys are used
- Test API calls succeed with custom keys, fail with wrong keys

**Scenario 4: End-to-End Application**
- Create application with auth enabled
- Upload document via Digitize UI
- Query via Chat UI
- Verify all operations work with authentication

### 8.2 Security Tests
- Verify keys have sufficient entropy (256 bits)
- Verify keys are stored with correct permissions
- Verify keys are not logged in plain text
- Test authentication actually works (401 vs 200)

### 8.3 Performance Tests
- Measure latency impact of authentication
- Load testing with authentication enabled
- Verify no connection leaks or excessive overhead

## 9. Migration Path

### 9.1 For New Deployments
- Authentication enabled by default
- Keys automatically generated
- No manual configuration required

### 9.2 For Existing Deployments
- Existing applications continue to work without changes
- `vllm.authEnabled` defaults to `true` in new deployments only
- Users can opt-in by updating values and redeploying
