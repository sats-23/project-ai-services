"""
Agentic RAG Orchestrator.

Implements a tool-call loop compatible with Granite 4.0's native tool-use format.
vLLM + Granite renders the `tools` list into <|available_tools|> automatically.
The loop:
  1. Send messages + tools to vLLM
  2. If the response has `tool_calls` → dispatch to the right agent, append result
  3. Loop until the model produces a plain text answer (no tool_calls)
  4. Return the final answer string
"""
import json
import time

import common.misc_utils as _misc
from common.misc_utils import get_logger
from common.sql_utils import get_schema_summary
from chatbot.tool_registry import TOOLS
import chatbot.vdb_agent as vdb_agent
import chatbot.sql_agent as sql_agent

logger = get_logger("orchestrator")

# Injected lazily so we don't import at module load (avoids circular import)
_schema_summary: str | None = None


def _get_schema_summary() -> str:
    global _schema_summary
    if _schema_summary is None:
        try:
            _schema_summary = get_schema_summary()
        except Exception as e:
            logger.warning(f"Could not load SQL schema: {e}")
            _schema_summary = "SQL schema unavailable."
    return _schema_summary


def _build_system_prompt() -> str:
    schema = _get_schema_summary()
    return (
        "You are a helpful assistant with access to two data sources:\n"
        "1. A vector knowledge base containing unstructured documents (text, tables, images).\n"
        "2. A relational SQL database (PostgreSQL) for structured data.\n\n"
        "When you need information, call the appropriate tool. "
        "You may call tools multiple times or in sequence if needed. "
        "Once you have gathered enough information, answer the user's question directly.\n\n"
        f"SQL Database Schema:\n{schema}"
    )


def _dispatch_tool(tool_name: str, tool_args: dict) -> str:
    """Call the appropriate sub-agent and return its result as a string."""
    if tool_name == "search_vector_store":
        query = tool_args.get("query", "")
        top_k = int(tool_args.get("top_k", 5))
        docs = vdb_agent.run(query, top_k=top_k)
        if not docs:
            return "No relevant documents found."
        # Format for readability in the tool result message
        parts = []
        for i, doc in enumerate(docs, 1):
            content = doc.get("page_content", "")
            fname = doc.get("filename", "")
            parts.append(f"[{i}] ({fname})\n{content}")
        return "\n\n".join(parts)

    if tool_name == "query_sql_database":
        sql = tool_args.get("sql", "")
        return sql_agent.run(sql)

    return f"Unknown tool: {tool_name}"


def run(
    question: str,
    llm_endpoint: str,
    llm_model: str,
    max_tokens: int = 1024,
    temperature: float = 0.1,
    api_key: str | None = None,
    previous_messages: list | None = None,
    max_iterations: int = 6,
) -> str:
    """Run the agentic tool-call loop and return the final answer.

    Args:
        question: The current user question.
        llm_endpoint: Base URL of the vLLM server.
        llm_model: Model name string.
        max_tokens: Max tokens for each LLM call.
        temperature: Sampling temperature.
        api_key: Optional vLLM API key.
        previous_messages: Prior conversation turns (list of role/content dicts).
        max_iterations: Safety cap on the tool-call loop.

    Returns:
        The model's final plain-text answer.
    """
    if _misc.SESSION is None:
        raise RuntimeError("LLM session not initialized. Call create_llm_session() first.")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Build the initial message list
    messages: list[dict] = [{"role": "system", "content": _build_system_prompt()}]

    if previous_messages:
        messages.extend(previous_messages)

    messages.append({"role": "user", "content": question})

    for iteration in range(max_iterations):
        logger.info(f"Orchestrator iteration {iteration + 1}/{max_iterations}")

        payload = {
            "model": llm_model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }

        t0 = time.time()
        response = _misc.SESSION.post(
            f"{llm_endpoint}/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        elapsed = time.time() - t0
        logger.info(f"LLM call took {elapsed:.2f}s")

        response_json = response.json()
        choice = response_json["choices"][0]
        finish_reason = choice.get("finish_reason", "")
        assistant_message = choice["message"]

        # Always append the assistant turn so the conversation is coherent
        messages.append(assistant_message)

        tool_calls = assistant_message.get("tool_calls")

        # No tool calls → model produced a final answer
        if not tool_calls or finish_reason == "stop":
            answer = assistant_message.get("content") or ""
            logger.info(f"Orchestrator finished after {iteration + 1} iteration(s)")
            return answer.strip()

        # Dispatch each tool call and append results
        for tc in tool_calls:
            tool_id = tc.get("id", f"call_{iteration}")
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"].get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_args = {}

            logger.info(f"Calling tool '{tool_name}' with args: {tool_args}")
            result = _dispatch_tool(tool_name, tool_args)
            logger.debug(f"Tool '{tool_name}' result (first 200 chars): {result[:200]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": result,
            })

    # Loop exhausted — return whatever the last assistant content was
    logger.warning("Orchestrator hit max_iterations, returning last assistant content")
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            return msg["content"].strip()
    return "I was unable to produce an answer within the allowed number of steps."
