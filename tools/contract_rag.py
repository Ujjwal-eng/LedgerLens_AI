"""
Traditional RAG

Unlike payment terms or price ranges, clauses like "how are emergency
callouts handled?" or "what's the late fee policy?" don't have one exact
field to look up — the relevant answer could be worded many ways
across different vendor contracts.

Uses a small local embedding model (no external API needed) for semantic
search over clauses.

Runs the SAME model as before (all-MiniLM-L6-v2) through fastembed / ONNX
Runtime instead of sentence-transformers / PyTorch. Same embeddings, far
less RAM — needed to fit Render's 512 MB free tier.
"""

import json
import os
import numpy as np
from fastembed import TextEmbedding

CONTRACTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "vendor_contracts.json")

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
# In Docker the model is downloaded at build time into this folder, so a
# cold start doesn't re-download ~90 MB. Unset locally -> fastembed default.
_CACHE_DIR = os.environ.get("FASTEMBED_CACHE_PATH")

_model = None  # lazy-loaded — importing this module shouldn't force a model load
_clause_cache: dict = {}  # tuple(clauses) -> embeddings; avoids re-embedding the same contract every call


def _get_model():
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=_CACHE_DIR)
    return _model


def _embed(texts: list) -> np.ndarray:
    # fastembed returns a generator of numpy vectors
    return np.array(list(_get_model().embed(texts)))


def _load_contracts():
    with open(CONTRACTS_PATH, "r") as f:
        return json.load(f)


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


def retrieve_relevant_clause(contract: dict, query: str, top_k: int = 1):
    if not contract or not contract.get("clauses"):
        return None
    clauses = contract["clauses"]

    key = tuple(clauses)
    if key not in _clause_cache:
        if len(_clause_cache) > 64:  # keep memory bounded
            _clause_cache.clear()
        _clause_cache[key] = _embed(clauses)
    clause_embeddings = _clause_cache[key]

    query_embedding = _embed([query])[0]

    scored = [
        (clauses[i], _cosine_sim(query_embedding, clause_embeddings[i]))
        for i in range(len(clauses))
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


if __name__ == "__main__":
    # quick manual smoke test
    contracts = _load_contracts()
    contract = next(
        (c for c in contracts if c["vendor_name"] == "Sharma Digital Solutions Pvt. Ltd."),
        contracts[0],
    )
    results = retrieve_relevant_clause(
        contract,
        "what happens if there's an emergency weekend callout not in the normal scope?",
    )
    for clause, score in results:
        print(f"[{score:.3f}] {clause}")
