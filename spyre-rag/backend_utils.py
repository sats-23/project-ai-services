import re
import time
import base64

from llm_utils import query_vllm
from reranker_utils import rerank_documents
from retrieval_utils import retrieve_documents, show_document_content, contains_chinese_regex


def search_and_answer_backend(
        question, llm_endpoint, llm_model, emb_model, emb_endpoint, max_tokens, reranker_model, reranker_endpoint,
        top_k, top_r, use_in_context, use_reranker, max_new_tokens, stop_words, language, vectorstore, deployment_type, stream, truncation
):

    print(f'Query language: {language}')
    # Perform retrieval
    retrieval_start = time.time()
    print("parameters")
    print(question)
    print(emb_model)
    print(emb_endpoint)

    retrieved_documents, retrieved_scores = retrieve_documents(question, emb_model, emb_endpoint, max_tokens, vectorstore, top_k, deployment_type, 'hybrid', language)
    print("retrieved")
    print(retrieved_documents)
    print("endpoint")
    print(reranker_endpoint)
    print(reranker_model)
    print(question)

    if use_reranker:
        reranked = rerank_documents(question, retrieved_documents, reranker_model, reranker_endpoint)

        print("reranked")
        print(reranked)
        ranked_documents = []
        ranked_scores = []
        for i, (doc, score) in enumerate(reranked, 1):
            ranked_documents.append(doc)
            ranked_scores.append(score)
            if i == top_r:
                break
        print("ranked")
        print("-----------------------")
        print(ranked_documents)
    else:
        ranked_documents = retrieved_documents[:top_r]
        ranked_scores = retrieved_scores[:top_r]
    retrieval_end = time.time()

    replacement_dict = {"케": "fi", "昀": "f", "椀": "i", "氀": "l"}
    for doc in ranked_documents:
        if contains_chinese_regex(doc["page_content"]):
            for key, val in replacement_dict.items():
                doc["page_content"] = re.sub(key, val, doc["page_content"])
    # Prepare stop words
    if stop_words:
        stop_words = stop_words.strip(' ').split(',')
        stop_words = [w.strip() for w in stop_words]
        stop_words = list \
            (set(stop_words) + set(['Context:', 'Question:', '\nContext:', '\nAnswer:', '\nQuestion:', 'Answer:']))
    else:
        stop_words = ['Context:', 'Question:', '\nContext:', '\nAnswer:', '\nQuestion:', 'Answer:']


    # RAG Answer Generation
    rag_answer, rag_generation_time = query_vllm(
        question, ranked_documents, llm_endpoint, llm_model, language, stop_words, max_new_tokens, rag=True, stream=stream, use_in_context=use_in_context,
        max_input_length=6000, dynamic_chunk_truncation=truncation
    )
    # rag_text = rag_answer.get('choices', [{}])[0].get('text', 'No RAG answer generated.')
    rag_text = rag_answer.get('choices', [{}])[0].get('message', 'No RAG answer generated.')['content']

    if rag_text == 'No RAG answer generated.':
        rag_text = rag_answer.get('response', 'No RAG answer generated.')
    #
    # rag_text = rag_answer.get('choices', [{}])[0].get('text', 'No RAG answer generated.')
    #
    # if rag_text == 'No RAG answer generated.':
    #     rag_text = rag_answer.get('response', 'No RAG answer generated.')

    return rag_text, ranked_documents


def search_only(question, emb_model, emb_endpoint, max_tokens, reranker_model, reranker_endpoint, top_k, top_r,
                use_reranker, language, vectorstore, deployment_type):
    print(f'Query language: {language}')
    # Perform retrieval
    retrieval_start = time.time()
    print("parameters")
    print(question)
    print(emb_model)
    print(emb_endpoint)

    retrieved_documents, retrieved_scores = retrieve_documents(question, emb_model, emb_endpoint, max_tokens,
                                                               vectorstore, top_k, deployment_type, 'hybrid', language)
    print("retrieved")
    print(retrieved_documents)
    print("endpoint")
    print(reranker_endpoint)
    print(reranker_model)
    print(question)

    if use_reranker:
        reranked = rerank_documents(question, retrieved_documents, reranker_model, reranker_endpoint)

        print("reranked")
        print(reranked)
        ranked_documents = []
        ranked_scores = []
        for i, (doc, score) in enumerate(reranked, 1):
            ranked_documents.append(doc)
            ranked_scores.append(score)
            if i == top_r:
                break
        print("ranked")
        print("-----------------------")
        print(ranked_documents)
    else:
        ranked_documents = retrieved_documents[:top_r]
        ranked_scores = retrieved_scores[:top_r]
    retrieval_end = time.time()
    replacement_dict = {"케": "fi", "昀": "f", "椀": "i", "氀": "l"}
    for doc in ranked_documents:
        if contains_chinese_regex(doc["page_content"]):
            for key, val in replacement_dict.items():
                doc["page_content"] = re.sub(key, val, doc["page_content"])
    return ranked_documents
