"""
Tool definitions for the Agentic RAG orchestrator.
Passed directly to vLLM as the `tools` field — Granite 4.0 renders these
into its <|available_tools|> block automatically via its chat template.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_vector_store",
            "description": (
                "Search the document knowledge base (vector store) for relevant "
                "unstructured content such as text, tables, and images. "
                "Use this when the question is about documents, policies, manuals, "
                "reports, or any free-text knowledge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to look up in the vector store."
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of chunks to retrieve. Defaults to 5.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_sql_database",
            "description": (
                "Execute a read-only SQL SELECT query against the structured relational "
                "database (PostgreSQL). Use this when the question requires precise "
                "structured data: counts, aggregations, filtering rows, joining tables, etc. "
                "Only SELECT statements are permitted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A valid PostgreSQL SELECT statement to execute."
                    }
                },
                "required": ["sql"],
            },
        },
    },
]

# Convenient lookup so the orchestrator can dispatch by name
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
