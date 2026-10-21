from typing import List, Tuple

from cohere import ClientV2
from cohere.core import RequestOptions

from common.misc_utils import get_logger
from common.retry_utils import retry_on_transient_error

logger = get_logger("reranker")


@retry_on_transient_error(max_retries=3, initial_delay=1.0, backoff_multiplier=2.0)
def rerank_documents(query: str, documents: List[dict], model: str, endpoint: str) -> List[Tuple[dict, float]]:
    """
    Rerank documents for a given query using vLLM-compatible Cohere API.

    Sends all documents in a single batch request to the reranker, which is more
    efficient than one-doc-at-a-time as it reduces HTTP round-trips from N to 1.

    Returns:
        List of (document, score) sorted by descending score.
    """
    co2 = ClientV2(api_key="sk-fake-key", base_url=endpoint)

    page_contents = [doc.get("page_content", "") for doc in documents]

    result = co2.rerank(
        model=model,
        query=query,
        documents=page_contents,
        max_tokens_per_doc=512,
        request_options=RequestOptions(timeout_in_seconds=60),
    )

    scored = [(documents[r.index], r.relevance_score) for r in result.results]
    return sorted(scored, key=lambda x: x[1], reverse=True)
