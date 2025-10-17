import os
import numpy as np
import hashlib
import joblib
from tqdm import tqdm
from scipy import sparse
from collections import defaultdict
from pymilvus import (
    connections, utility, Collection, CollectionSchema,
    FieldSchema, DataType
)
from sklearn.feature_extraction.text import TfidfVectorizer
from emb_utils import FastAPIEmbeddingFunction


def generate_chunk_id(filename: str, page_content: str, index: int) -> int:
    """
    Generate a unique, deterministic chunk ID based on filename, content, and index.
    """
    base = f"{filename}-{index}-{page_content}"
    hash_digest = hashlib.md5(base.encode("utf-8")).hexdigest()
    chunk_int = int(hash_digest[:16], 16)  # Convert first 64 bits to int
    chunk_id = chunk_int % (2**63)  # Fit into signed 64-bit range
    return np.int64(chunk_id)


class MilvusVectorStore:
    def __init__(
        self,
        host="localhost",
        port="19530",
        db_prefix="RAG_DB",
        emb_name=None,
        vlm_name=None,
        llm_name=None
    ):
        self.host = host
        self.port = port
        self.db_prefix = db_prefix
        self.emb_name = emb_name
        self.vlm_name = vlm_name
        self.llm_name = llm_name

        connections.connect("default", host=self.host, port=self.port)

        self.collection = None
        self.collection_name = None
        self._embedder = None
        self._embedder_config = {}

        self.page_content_corpus = []
        self.metadata_map = []
        self.vectorizer = None
        self.sparse_matrix = None

    def _generate_collection_name(self):
        base = f"{self.emb_name}_{self.vlm_name}_{self.llm_name}"
        hash_part = hashlib.md5(base.encode()).hexdigest()
        return f"{self.db_prefix}_{hash_part}"

    def _get_index_paths(self):
        base_path = f"{self.collection_name}_sparse_index"
        return f"{base_path}_vectorizer.joblib", f"{base_path}_matrix.npz", f"{base_path}_metadata.joblib"

    def _save_sparse_index(self):
        vectorizer_path, matrix_path, metadata_path = self._get_index_paths()
        joblib.dump(self.vectorizer, vectorizer_path)
        sparse.save_npz(matrix_path, self.sparse_matrix)
        joblib.dump(self.metadata_map, metadata_path)

    def _load_sparse_index(self):
        vectorizer_path, matrix_path, metadata_path = self._get_index_paths()
        if os.path.exists(vectorizer_path) and os.path.exists(matrix_path) and os.path.exists(metadata_path):
            self.vectorizer = joblib.load(vectorizer_path)
            self.sparse_matrix = sparse.load_npz(matrix_path)
            self.metadata_map = joblib.load(metadata_path)
            print(f"✅ Loaded sparse index for collection '{self.collection_name}'.")
            return True
        return False

    def _setup_collection(self, name, dim):
        if utility.has_collection(name):
            return Collection(name=name)

        fields = [
            FieldSchema(name="chunk_id", dtype=DataType.INT64, is_primary=True, auto_id=False),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="page_content", dtype=DataType.VARCHAR, max_length=32768, enable_analyzer=True),
            FieldSchema(name="filename", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="type", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=32768),
            FieldSchema(name="language", dtype=DataType.VARCHAR, max_length=8),
        ]

        schema = CollectionSchema(fields=fields, description="RAG chunk storage (dense only)")
        collection = Collection(name=name, schema=schema)

        collection.create_index(
            field_name="embedding",
            index_params={"metric_type": "L2", "index_type": "IVF_FLAT", "params": {"nlist": 128}}
        )

        return collection

    def _ensure_embedder(self, emb_model, emb_endpoint, max_tokens, deployment_type):
        config = {"model": emb_model, "endpoint": emb_endpoint, "max_tokens": max_tokens}
        if self._embedder is None or self._embedder_config != config:
            print(f"⚙️ Initializing embedder: {emb_model}")
            self._embedder = FastAPIEmbeddingFunction(emb_model, emb_endpoint, max_tokens, deployment_type)
            self._embedder_config = config

    def reset_collection(self):
        name = self._generate_collection_name()
        if utility.has_collection(name):
            utility.drop_collection(name)
            print(f"✅ Collection {name} deleted.")
        else:
            print(f"ℹ️ Collection {name} does not exist.")

        self.page_content_corpus = []
        self.metadata_map = []
        self.vectorizer = None
        self.sparse_matrix = None

    def insert_chunks(self, emb_model, emb_endpoint, max_tokens, chunks, deployment_type, batch_size=10):
        self._ensure_embedder(emb_model, emb_endpoint, max_tokens, deployment_type)
        self.collection_name = self._generate_collection_name()

        sample_embedding = self._embedder.embed_documents([chunks[0]["page_content"]])[0]
        dim = len(sample_embedding)

        self.collection = self._setup_collection(self.collection_name, dim)
        self.collection.load()

        print(f"Inserting {len(chunks)} chunks into Milvus...")

        for i in tqdm(range(0, len(chunks), batch_size), desc='Ingesting Data into Vector DB'):
            batch = chunks[i:i + batch_size]
            page_contents = [doc.get("page_content") for doc in batch]
            embeddings = self._embedder.embed_documents(page_contents)

            filenames = [doc.get("filename", "") for doc in batch]
            types = [doc.get("type", "") for doc in batch]
            sources = [doc.get("source", "") for doc in batch]
            languages = [doc.get("language", "") for doc in batch]

            chunk_ids = [generate_chunk_id(fn, pc, i+j) for j, (fn, pc) in enumerate(zip(filenames, page_contents))]

            self.collection.insert([
                chunk_ids,
                embeddings,
                page_contents,
                filenames,
                types,
                sources,
                languages
            ])

            self.page_content_corpus.extend(page_contents)
            self.metadata_map.extend([
                {"chunk_id": cid, "filename": fn, "type": t, "source": s, "page_content": pc, "language": l}
                for cid, fn, t, s, pc, l in zip(chunk_ids, filenames, types, sources, page_contents, languages)
            ])

        print("Fitting external BM25 (TF-IDF)...")
        self.vectorizer = TfidfVectorizer()
        self.sparse_matrix = self.vectorizer.fit_transform(self.page_content_corpus)

        self._save_sparse_index()
        print(f"✅ Inserted {len(chunks)} chunks into collection '{self.collection_name}'.")

    def _rrf_fusion(self, dense_results, sparse_results, top_k):
        """
        Perform Reciprocal Rank Fusion (RRF) on dense and sparse results.
        Each result should be a list of dicts with at least 'chunk_id' field.
        """
        rrf_k = 60  # RRF constant to dampen higher ranks
        score_map = defaultdict(float)
        doc_map = {}

        # Process dense results
        for rank, doc in enumerate(dense_results):
            cid = doc["chunk_id"]
            score_map[cid] += 1 / (rank + 1 + rrf_k)
            doc_map[cid] = doc  # Store full metadata

        # Process sparse results
        for rank, doc in enumerate(sparse_results):
            cid = doc["chunk_id"]
            score_map[cid] += 1 / (rank + 1 + rrf_k)
            doc_map[cid] = doc  # Will overwrite if duplicate, but that's fine

        # Sort by combined RRF score
        sorted_items = sorted(score_map.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # Assemble final results
        final_results = []
        for cid, score in sorted_items:
            result = doc_map[cid].copy()
            result["rrf_score"] = score
            final_results.append(result)

        return final_results

    def search(self, query, emb_model, emb_endpoint, max_tokens, top_k=5, deployment_type='cpu', mode="hybrid", language='en'):
        self._ensure_embedder(emb_model, emb_endpoint, max_tokens, deployment_type)
        self.collection_name = self._generate_collection_name()

        if not utility.has_collection(self.collection_name):
            raise ValueError(f"❌ Collection '{self.collection_name}' does not exist in Milvus.")

        query_vector = self._embedder.embed_query(query)
        self.collection = Collection(name=self.collection_name)
        self.collection.load()

        if mode == "dense":
            results = self.collection.search(
                data=[query_vector],
                anns_field="embedding",
                param={"metric_type": "L2", "params": {"nprobe": 10}},
                limit=top_k * 3,  # retrieve more for filtering
                output_fields=["chunk_id", "page_content", "filename", "type", "source", "language"],
                expr=f"language == \"{language}\"" if language else None
            )
            dense_results = [hit.get('entity') for hit in results[0]]
            dense_results = dense_results[:top_k]
            
            return dense_results

        elif mode == "sparse":
            if self.vectorizer is None or self.sparse_matrix is None:
                loaded = self._load_sparse_index()
                if not loaded:
                    raise RuntimeError("Sparse search index not initialized.")

            query_vec = self.vectorizer.transform([query])
            scores = (self.sparse_matrix @ query_vec.T).toarray().ravel()
            ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:3*top_k] # retrieve more for filtering
            sparse_results = []
            for idx, score in ranked:
                metadata = self.metadata_map[idx]
                if language is None or metadata.get("language") == language:
                    sparse_results.append({**metadata, "score": score})
                if len(sparse_results) >= top_k:
                    break
            
            return sparse_results

        elif mode == "hybrid":
            if self.vectorizer is None or self.sparse_matrix is None:
                loaded = self._load_sparse_index()
                if not loaded:
                    raise RuntimeError("Sparse index missing for hybrid search.")

            dense_results = self.collection.search(
                data=[query_vector],
                anns_field="embedding",
                param={"metric_type": "L2", "params": {"nprobe": 10}},
                limit=top_k * 3,  # retrieve more for filtering
                output_fields=["chunk_id", "page_content", "filename", "type", "source", "language"],
                expr=f"language == \"{language}\"" if language else None
            )
            dense_results = [hit.get('entity') for hit in dense_results[0]]
            dense_results = dense_results[:top_k]

            query_vec = self.vectorizer.transform([query])
            scores = (self.sparse_matrix @ query_vec.T).toarray().ravel()
            sparse_ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:3*top_k] # retrieve more for filtering

            sparse_results = []
            for idx, score in sparse_ranked:
                metadata = self.metadata_map[idx]
                if language is None or metadata.get("language") == language:
                    sparse_results.append({**metadata, "score": score})
                if len(sparse_results) >= top_k:
                    break

            return self._rrf_fusion(dense_results, sparse_results, top_k)

        else:
            raise ValueError("Invalid search mode. Choose from ['dense', 'sparse', 'hybrid'].")


class VectorStoreManager:
    def __init__(self):
        self.vectorstore = None
        self.last_config = {}

    def initialize_vectorstore(self, emb_name, vlm_name, llm_name, db_name_prefix):
        self.vectorstore = MilvusVectorStore(db_prefix=db_name_prefix, emb_name=emb_name, vlm_name=vlm_name, llm_name=llm_name)
        self.last_config = {
            "emb": emb_name,
            "vlm": vlm_name,
            "llm": llm_name,
            "db_prefix": db_name_prefix
        }
        return self.vectorstore
