# Agentic RAG — Session Summary
> Use this file to resume the implementation conversation with Bob.

---

## Project
**Repo:** `project-ai-services`  
**Active workspace:** `/Users/sats/Code/project-ai-services`  
**Branch:** `main`

---

## What We Did

### Starting Point
The project had a classic **linear RAG** pipeline:

```
User → FastAPI Chatbot → Similarity Service (OpenSearch VDB) → LLM → Response
```

Key existing files:
- `services/chatbot/app.py` — FastAPI app, existing `/v1/chat/completions` endpoint
- `services/chatbot/backend_utils.py` — `search_only()` calls the similarity HTTP service
- `services/chatbot/settings.py` — Pydantic `Settings` with `RAGConfig`, `LLMConfig`, etc.
- `services/common/retrieval_utils.py` — `retrieve_documents()`, one VDB path
- `services/common/vector_db.py` — abstract `VectorStore` ABC
- `services/common/db_utils.py` — factory: only `OPENSEARCH` supported
- `services/common/db/connection.py` — full PostgreSQL SQLAlchemy engine factory (already existed)
- `services/common/llm_utils.py` — `query_vllm_payload()`, `query_vllm_non_stream()`, `query_vllm_stream()`
- `services/common/misc_utils.py` — global `SESSION` (requests.Session), `create_llm_session()`

### Goal
Convert to **Agentic RAG with multi-source retrieval** (matching the architecture diagram):
- A **Manager/Coordinator Agent** (orchestrator) that plans and dispatches tool calls
- A **VDB Retrieval Agent** wrapping the existing OpenSearch pipeline
- A **SQL Database Agent** querying PostgreSQL with safe SELECT-only execution
- New endpoint **`/v1/agent/chat`** alongside the existing `/v1/chat/completions` (both run in parallel for demo)

### Key Technical Decision: Granite 4.0 Tool-Use Format
Granite 4.0's chat template (confirmed from HuggingFace tokenizer_config.json) **natively supports OpenAI-style tool calling** via vLLM `/v1/chat/completions`. The template:
- Accepts `tools` array in the payload → renders into `<|available_tools|>` block automatically
- Model emits `tool_calls` in assistant messages (OpenAI format) when it wants to call a tool
- Tool results are fed back as `{"role": "tool", "tool_call_id": ..., "content": ...}` messages
- **No custom prompt-based ReAct loop needed** — use standard OpenAI tool-call protocol

---

## Files Created / Modified

### New Files

#### `services/chatbot/tool_registry.py`
OpenAI-style tool schemas for two tools:
- `search_vector_store` — searches the VDB knowledge base
- `query_sql_database` — runs a PostgreSQL SELECT query

#### `services/common/sql_utils.py`
Safe read-only SQL executor:
- `get_schema_summary()` — introspects `public` schema tables/columns for the LLM system prompt
- `execute_select(sql)` — validates SELECT-only, runs via SQLAlchemy, returns `list[dict]`
- Uses the existing `common/db/connection.py` engine factory (reads `POSTGRES_*` env vars)

#### `services/chatbot/vdb_agent.py`
Thin wrapper around `backend_utils.search_only()`:
- `run(query, top_k)` → calls existing `search_only()`, returns list of chunk dicts
- Zero new infrastructure — reuses OpenSearch + embeddings + reranker as-is

#### `services/chatbot/sql_agent.py`
SQL agent callable:
- `run(sql)` → calls `sql_utils.execute_select()`, returns JSON string
- Caps at 50 rows to keep context manageable
- Returns `{"error": "..."}` JSON on failure (safe for LLM consumption)

#### `services/chatbot/orchestrator_agent.py`
The brain — vLLM tool-call loop:
- Builds system prompt dynamically including SQL schema summary
- Sends `messages + tools + tool_choice="auto"` to vLLM
- Loops: if response has `tool_calls` → dispatch to `vdb_agent` or `sql_agent`, append `tool` message, loop
- Stops when model produces a plain answer (no `tool_calls`) or `finish_reason == "stop"`
- Safety cap: `max_iterations` (default 6, configurable via `AGENT_MAX_ITERATIONS` env var)
- Uses the global `_misc.SESSION` (shared requests.Session from `common.misc_utils`)

### Modified Files

#### `services/chatbot/settings.py`
Added `AgentConfig` class (env prefix `AGENT_`):
```python
class AgentConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_")
    max_iterations: int = Field(default=6)
    enabled: bool = Field(default=True)
```
Added `agent: AgentConfig` field to `Settings`.

#### `services/common/db_utils.py`
Added `get_sql_engine()` factory that calls `common/db/connection.py`'s `create_db_engine()`.

#### `services/chatbot/app.py`
Added new `POST /v1/agent/chat` endpoint at line ~710 (before `/db-status`):
- Same request model as `/v1/chat/completions` (`ChatCompletionRequest`)
- Same response model (`ChatCompletionResponse`)
- Calls `orchestrator_agent.run()` via `asyncio.to_thread` (keeps FastAPI async)
- Uses `@limit_concurrency` decorator (same as existing endpoint)
- **Does NOT stream** in this PoC (streaming would require splitting the orchestrator's final LLM call)

---

## Environment Variables Required for SQL Side

```bash
POSTGRES_HOST=localhost        # or your Docker container name
POSTGRES_PORT=5432
POSTGRES_DB=your_database
POSTGRES_USER=your_user
POSTGRES_PASSWORD=your_password
```

The VDB side already works from existing env vars (OpenSearch, embedding endpoint, etc.).

---

## What Still Works Unchanged
| Component | Status |
|---|---|
| `GET/POST /v1/chat/completions` | ✅ Untouched — existing RAG still works |
| OpenSearch VDB + embeddings | ✅ Reused by `vdb_agent.py` |
| Reranker | ✅ Still applied inside `search_only()` |
| Language detection + rephrasing | ✅ Still in existing endpoint |
| All existing tests | ✅ No existing files broken |

---

## What's NOT Done Yet (Next Steps)
- **Streaming for `/v1/agent/chat`** — the final LLM call in the orchestrator could be made streaming; the tool-call iterations themselves are always non-streaming
- **API/Remote App Agent** — third agent from the diagram (calls external APIs via MCP tools); not implemented
- **Tests** — intentionally skipped for PoC
- **Schema caching** — `get_schema_summary()` is cached per-process but re-fetched on restart; could be made configurable
- **Auth enforcement on agent endpoint** — currently follows the same `is_auth_required()` check as the existing endpoint

---

## Architecture Diagram (as implemented)

```
User
 │
 ▼  POST /v1/agent/chat
FastAPI app.py  →  orchestrator_agent.run()
                        │
                        │  messages + tools → vLLM (Granite 4.0)
                        │  ← tool_calls response
                        │
                   ┌────┴─────────────────────────┐
                   │                              │
             vdb_agent.run()              sql_agent.run()
                   │                              │
           backend_utils                   sql_utils
           search_only()               execute_select()
                   │                              │
             OpenSearch VDB               PostgreSQL
```

---

## How to Demo

```bash
# Start the chatbot service as normal (existing startup)
# Ensure POSTGRES_* env vars are set

# Existing RAG (unchanged)
curl -X POST http://localhost:5000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is in the documents?"}]}'

# New Agentic RAG
curl -X POST http://localhost:5000/v1/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "How many records are in the orders table and what do the related policy documents say about returns?"}]}'
```

The second query will cause Granite to call both `query_sql_database` AND `search_vector_store`, combine the results, and answer.
