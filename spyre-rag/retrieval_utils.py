import time
import base64
import regex as re

from llm_utils import query_vllm
from reranker_utils import rerank_documents


def contains_chinese_regex(text):
    return bool(re.search(r'\p{Han}', text))


def format_table_html(table_html):
    """
    Ensures that the table HTML is properly formatted.
    This is a basic check to wrap the table inside a <table> tag if it isn't already wrapped.
    """
    if not table_html.startswith("<table"):
        table_html = f"<table>{table_html}</table>"
    return table_html

def show_document_content(retrieved_documents, scores):
    html_content = ""
    
    for idx, (doc, score) in enumerate(zip(retrieved_documents, scores)):
        doc_type = doc.get("type")
        
        # Document Header with Score
        document_header = f'<h4>Document {idx + 1} (Score: {score:.4f}), (Doc: {doc.get("filename")})</h4>'
        html_content += document_header
        
        # If the document is an image
        if doc_type == "image":
            image_path = doc.get("source")
            with open(image_path, "rb") as image_file:
                encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
            image_html = f'<div style="border: 1px solid #ccc; padding: 10px; background-color: #f0f0f0; width: 100%; margin-top: 20px;">'
            image_html += f'<img src="data:image/jpeg;base64,{encoded_string}" alt="Image {doc.get("chunk_id")}" style="width: 50%; height: auto;" />'
            image_summary = f'<p><strong>Image Summary:</strong> {doc.get("page_content")}</p>'
            image_html += f'{image_summary}</div>'
            html_content += image_html

        # If the document is a table
        elif doc_type == "table":
            table_html = doc.get("source")
            if table_html:
                table_html = format_table_html(table_html)  # Ensure proper HTML wrapping
                table_summary = f'<p><strong>Table Summary:</strong> {doc.get("page_content")}</p>'
                html_content += f'<div style="margin-top: 20px; border: 1px solid #ccc; padding: 10px; background-color: #f0f0f0;">{table_html}<br>{table_summary}</div>'

        # If the document is plain text
        elif doc_type == "text":
            converted_doc_string = doc.get("page_content").replace("\n", "<br>")
            html_content += f'<div style="margin-top: 20px; padding: 10px; border: 1px solid #ccc; background-color: #f0f0f0;">{converted_doc_string}</div>'

    return html_content


def retrieve_documents(query, emb_model, emb_endpoint, max_tokens, vectorstore, top_k, deployment_type, mode="hybrid", language='en'):
    results = vectorstore.search(query, emb_model, emb_endpoint, max_tokens, top_k, deployment_type, mode=mode, language=language)

    retrieved_documents = []
    scores = []

    for hit in results:
        doc = {
            "page_content": hit.get("page_content", ""),
            "filename": hit.get("filename", ""),
            "type": hit.get("type", ""),
            "source": hit.get("source", ""),
            "chunk_id": hit.get("chunk_id", "")
        }
        retrieved_documents.append(doc)

        # For dense hits from Milvus, we expect `.score` or `.distance`.
        score = hit.get("rrf_score") or hit.get("score") or hit.get("distance") or 0.0
        scores.append(score)

    return retrieved_documents, scores


def search_and_answer_dual(
        question, llm_endpoint, llm_model, emb_model, emb_endpoint, max_tokens, reranker_model, reranker_endpoint, 
        top_k, top_r, stop_words, language, vectorstore, deployment_type, stream
    ):
    
    print(f'Query language: {language}')
    # Perform retrieval
    retrieval_start = time.time()
    print("parameters")
    print(question)
    print(emb_model)
    print(emb_endpoint)

    retrieved_documents, scores = retrieve_documents(question, emb_model, emb_endpoint, max_tokens, vectorstore, top_k, deployment_type, 'hybrid', language)
    print("retrieved")
    print(retrieved_documents)
    print("endpoint")
    print(reranker_endpoint)
    print(reranker_model)
    print(question)
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
    retrieval_end = time.time()
    print("ranked")
    print("-----------------------")
    print(ranked_documents) 
    # Prepare stop words
    if stop_words:
        stop_words = stop_words.strip(' ').split(',')
        stop_words = [w.strip() for w in stop_words]
        stop_words = list(set(stop_words) + set(['Context:', 'Question:', '\nContext:', '\nAnswer:', '\nQuestion:', 'Answer:']))
    else:
        stop_words = ['Context:', 'Question:', '\nContext:', '\nAnswer:', '\nQuestion:', 'Answer:']
    
    # Call show_document_content to format retrieved documents
    html_content = show_document_content(ranked_documents, ranked_scores)
    
    # RAG Answer Generation
    rag_answer, rag_generation_time = query_vllm(
        question, ranked_documents, llm_endpoint, llm_model, language, stop_words, rag=True, stream=stream
    )
    
    # No-RAG Answer Generation
    no_rag_answer, no_rag_generation_time = query_vllm(
        question, [], llm_endpoint, llm_model, language, stop_words, rag=False, stream=stream
    )
    
    rag_text = rag_answer.get('choices', [{}])[0].get('text', 'No RAG answer generated.')
    no_rag_text = no_rag_answer.get('choices', [{}])[0].get('text', 'No No-RAG answer generated.')

    if rag_text == 'No RAG answer generated.':
        rag_text = rag_answer.get('response', 'No RAG answer generated.')
        no_rag_text = no_rag_answer.get('response', 'No No-RAG answer generated.')
    
    return (
        f"<h3>RAG Answer (Generation Time - {rag_generation_time:.2f} seconds):</h3><p>{rag_text}</p>",
        f"<h3>No-RAG Answer (Generation Time - {no_rag_generation_time:.2f} seconds):</h3><p>{no_rag_text}</p>",
        f"<h3>Top Documents (Retrieval and Reranking Time - {retrieval_end - retrieval_start:.2f} seconds):</h3>{html_content}",
    )


def search_and_answer(
        question, llm_endpoint, llm_model, emb_model, emb_endpoint, max_tokens, reranker_model, reranker_endpoint, 
        top_k, top_r, use_in_context, use_reranker, max_new_tokens, stop_words, language, vectorstore, deployment_type, stream
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
        stop_words = list(set(stop_words) + set(['Context:', 'Question:', '\nContext:', '\nAnswer:', '\nQuestion:', 'Answer:']))
    else:
        stop_words = ['Context:', 'Question:', '\nContext:', '\nAnswer:', '\nQuestion:', 'Answer:']
    
    # Call show_document_content to format retrieved documents
    html_content = show_document_content(ranked_documents, ranked_scores)
    
    # RAG Answer Generation
    rag_answer, rag_generation_time = query_vllm(
        question, ranked_documents, llm_endpoint, llm_model, language, stop_words, max_new_tokens, rag=True, stream=stream, use_in_context=use_in_context
    )
    
    # rag_text = rag_answer.get('choices', [{}])[0].get('text', 'No RAG answer generated.')
    rag_text = rag_answer.get('choices', [{}])[0].get('message', 'No RAG answer generated.')['content']

    if rag_text == 'No RAG answer generated.':
        rag_text = rag_answer.get('response', 'No RAG answer generated.')
    
    return (
        f"<h3>RAG Answer (Generation Time - {rag_generation_time:.2f} seconds):</h3><p>{rag_text}</p>",
        f"<h3>Top Documents (Retrieval and Reranking Time - {retrieval_end - retrieval_start:.2f} seconds):</h3>{html_content}",
    )
