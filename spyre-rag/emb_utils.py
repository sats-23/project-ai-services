import json
import ollama
import requests
import numpy as np


with open("api_key.txt", "r") as file:
    api_key = file.read().strip()


class FastAPIEmbeddingFunction:
    def __init__(self, emb_model, emb_endpoint, max_tokens, deployment_type='cpu'):
        self.emb_model = emb_model
        self.emb_endpoint = emb_endpoint
        self.max_tokens = max_tokens
        self.deployment_type = deployment_type.lower()
        self.client = ollama.Client(host=self.emb_endpoint) if self.deployment_type == 'cpu' or self.deployment_type == 'spyre' else None

    def embed_documents(self, texts):
        if self.deployment_type == 'cpu' or self.deployment_type == 'spyre':
            # embeddings = [np.array(self.client.embed(model=self.emb_model, input=text, truncate=True)["embeddings"][0], dtype=np.float32) for text in texts]
            emb_vecs = self.client.embed(model=self.emb_model, input=texts, truncate=True)["embeddings"]
            embeddings = [np.array(vec, dtype=np.float32) for vec in emb_vecs]
            return embeddings
        else:
            return self._call_fastapi_embedding(texts)

    def embed_query(self, text):
        if self.deployment_type == 'cpu' or self.deployment_type == 'spyre':
            return np.array(self.client.embed(model=self.emb_model, input=text, truncate=True)["embeddings"][0], dtype=np.float32)
        else:
            return self._call_fastapi_embedding([text])[0]

    def _call_fastapi_embedding(self, texts):
        payload = {
            "input": texts,
            "model": self.emb_model,
            "truncate_prompt_tokens": self.max_tokens-1,
        }
        headers = {
            "accept": "application/json",
            "RITS_API_KEY": api_key,
            "Content-type": "application/json"
        }
        response = requests.post(
            self.emb_endpoint,
            data=json.dumps(payload),
            headers=headers
        )
        response.raise_for_status()
        r = response.json()
        embeddings = [data['embedding'] for data in r['data']]
        return [np.array(embed, dtype=np.float32) for embed in embeddings]
