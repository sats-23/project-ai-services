import json

from flask import Flask, request, jsonify, Response, stream_with_context
import time
import requests
from pymilvus import connections
from db_utils import MilvusVectorStore, VectorStoreManager
from misc_utils import get_model_endpoints
from backend_utils import search_and_answer_backend, search_only
from llm_utils import query_vllm_stream

MILVUS_HOST = "localhost"
MILVUS_PORT = "19530"
connections.connect("default", host=MILVUS_HOST, port=MILVUS_PORT)

vectorstore = None
DEPLOYMENT_TYPE = 'spyre'
EMB_PREFIX      = 'EMB'
LLM_PREFIX      = 'LLM'
RERANKER_PREFIX = 'RER'
DB_NAME_PREFIX  = 'MLV'
TRUNCATION      = True

# Globals to be set dynamically
emb_model_dict = {}
llm_model_dict = {}
reranker_model_dict = {}

# Keep track of the last used config for vectorstore
vector_store_manager = VectorStoreManager()

def initialize_models(deployment_type):
    global emb_model_dict, llm_model_dict, reranker_model_dict
    emb_model_dict, llm_model_dict, reranker_model_dict = get_model_endpoints(deployment_type)

def initialize_vectorstore_if_needed(db_name_prefix):
    current_config = {
        "emb": EMB_PREFIX,
        "llm": LLM_PREFIX,
        "db_prefix": db_name_prefix,
    }

    config_changed = current_config != vector_store_manager.last_config

    if vector_store_manager.vectorstore is None or config_changed:
        print("🔄 Reinitializing vectorstore due to config change...")
        vectorstore = MilvusVectorStore(
            host=MILVUS_HOST,
            port=MILVUS_PORT,
            db_prefix=db_name_prefix,
            emb_name=EMB_PREFIX,
            llm_name=LLM_PREFIX
        )
        vector_store_manager.vectorstore = vectorstore
        vector_store_manager.last_config = current_config
        return vectorstore

    print("✅ Reusing existing vectorstore.")
    return vector_store_manager.vectorstore


app = Flask(__name__)


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json()
    prompt = data.get("prompt", "")
    num_chunks_post_rrf = data.get("num_chunks_post_rrf", 10)
    num_docs_reranker = data.get("num_docs_reranker", 3)
    use_in_context = data.get("use_in_context", False)
    use_reranker = data.get("use_reranker", True)
    max_tokens = data.get("max_tokens", 512)
    start_time = time.time()
    try:
        emb_model = emb_model_dict['emb_model']
        emb_endpoint = emb_model_dict['emb_endpoint']
        emb_max_tokens = emb_model_dict['max_tokens']
        llm_model = llm_model_dict['llm_model']
        llm_endpoint = llm_model_dict['llm_chat_endpoint']
        reranker_model = reranker_model_dict['reranker_model']
        reranker_endpoint = reranker_model_dict['reranker_endpoint']

        vectorstore = initialize_vectorstore_if_needed(DB_NAME_PREFIX)

        stop_words = ""

        (rag_ans, docs) = search_and_answer_backend(
            prompt,
            llm_endpoint,
            llm_model,
            emb_model, emb_endpoint, emb_max_tokens,
            reranker_model,
            reranker_endpoint,
            num_chunks_post_rrf,
            num_docs_reranker,
            use_in_context,
            use_reranker,
            max_tokens,
            stop_words=stop_words,
            language="en",
            vectorstore=vectorstore,
            deployment_type=DEPLOYMENT_TYPE,
            stream=False,
            truncation=TRUNCATION
        )
    except Exception as e:
        return jsonify({"error": repr(e)}), 500
    end_time = time.time()
    request_time = end_time - start_time
    return Response(
        json.dumps({"response": rag_ans, "documents": docs, "request time": request_time}, default=str),
        mimetype="application/json"
    )

@app.route("/stream", methods=["POST"])
def stream():
    data = request.get_json()
    prompt = data.get("prompt", "")
    num_chunks_post_rrf = data.get("num_chunks_post_rrf", 10)
    num_docs_reranker = data.get("num_docs_reranker", 3)
    use_in_context = data.get("use_in_context", False)
    use_reranker = data.get("use_reranker", True)
    max_tokens = data.get("max_tokens", 512)
    try:
        emb_model = emb_model_dict['emb_model']
        emb_endpoint = emb_model_dict['emb_endpoint']
        emb_max_tokens = emb_model_dict['max_tokens']
        llm_model = llm_model_dict['llm_model']
        llm_endpoint = llm_model_dict['llm_chat_endpoint']
        reranker_model = reranker_model_dict['reranker_model']
        reranker_endpoint = reranker_model_dict['reranker_endpoint']

        vectorstore = initialize_vectorstore_if_needed(DB_NAME_PREFIX)
        lang = detect_en_hi(prompt)
        docs = search_only(
            prompt,
            emb_model, emb_endpoint, emb_max_tokens,
            reranker_model,
            reranker_endpoint,
            num_chunks_post_rrf,
            num_docs_reranker,
            use_reranker,
            language=lang,
            vectorstore=vectorstore,
            deployment_type=DEPLOYMENT_TYPE,
        )
    except Exception as e:
        return jsonify({"error": repr(e)})

    stop_words = ""
    if stop_words:
        stop_words = stop_words.strip(' ').split(',')
        stop_words = [w.strip() for w in stop_words]
        stop_words = list \
            (set(stop_words) + set(['Context:', 'Question:', '\nContext:', '\nAnswer:', '\nQuestion:', 'Answer:']))
    else:
        stop_words = ['Context:', 'Question:', '\nContext:', '\nAnswer:', '\nQuestion:', 'Answer:']
    return Response(stream_with_context(query_vllm_stream(prompt, docs, llm_endpoint, llm_model, lang, stop_words,
                                                          max_tokens, True, True, use_in_context, dynamic_chunk_truncation=TRUNCATION)),
                    content_type='text/plain',
                    mimetype='text/event-stream', headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type'
        })




@app.route("/reference", methods=["POST"])
def get_reference_docs():
    data = request.get_json()
    prompt = data.get("prompt", "")
    num_chunks_post_rrf = data.get("num_chunks_post_rrf", 10)
    num_docs_reranker = data.get("num_docs_reranker", 3)
    use_reranker = data.get("use_reranker", True)
    try:
        emb_model = emb_model_dict['emb_model']
        emb_endpoint = emb_model_dict['emb_endpoint']
        emb_max_tokens = emb_model_dict['max_tokens']
        reranker_model = reranker_model_dict['reranker_model']
        reranker_endpoint = reranker_model_dict['reranker_endpoint']

        vectorstore = initialize_vectorstore_if_needed(DB_NAME_PREFIX)
        lang = detect_en_hi(prompt)
        docs = search_only(
            prompt,
            emb_model, emb_endpoint, emb_max_tokens,
            reranker_model,
            reranker_endpoint,
            num_chunks_post_rrf,
            num_docs_reranker,
            use_reranker,
            language=lang,
            vectorstore=vectorstore,
            deployment_type=DEPLOYMENT_TYPE,
        )
    except Exception as e:
        return jsonify({"error": repr(e)})
    return Response(
        json.dumps({"documents": docs}, default=str),
        mimetype="application/json"
    )

if __name__ == "__main__":
    initialize_models("spyre")
    app.run(host="0.0.0.0", port=8001)
