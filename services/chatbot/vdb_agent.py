"""
VDB Retrieval Agent — wraps the existing similarity search pipeline.
Called by the orchestrator when the LLM chooses the `search_vector_store` tool.
"""
from common.misc_utils import get_logger
from chatbot.backend_utils import search_only
from chatbot.settings import settings

logger = get_logger("vdb_agent")


def run(query: str, top_k: int = 5) -> list[dict]:
    """Search the vector store and return matching document chunks.

    Args:
        query: The search query string.
        top_k: How many chunks to retrieve (and return, reranking is handled
               inside search_only via the existing rerank flag in settings).

    Returns:
        List of document chunk dicts with page_content, filename, type, etc.
    """
    top_r = min(top_k, settings.chatbot.num_chunks_post_reranker)
    logger.info(f"VDB search: top_k={top_k}, top_r={top_r}, query={query[:80]!r}")
    docs, _ = search_only(query, top_k=top_k, top_r=top_r)
    logger.info(f"VDB search returned {len(docs)} chunks")
    return docs
