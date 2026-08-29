"""
Traditional RAG

Unlike payment terms or price ranges, clauses like "how are emergency
callouts handled?" or "what's the late fee policy?" don't have one exact
field to look up — the relevant answer could be worded many ways
across different vendor contracts.

Uses a small local sentence-transformer model (no external API needed) for semantic search over clauses.
"""

import json
import os
import numpy as np
from sentence_transformers import SentenceTransformer

CONTRACTS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "vendor_contracts.json")

_model = None  # lazy-loaded — importing this module shouldn't force a model download


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _load_contracts():
    with open(CONTRACTS_PATH, "r") as f:
        return json.load(f)


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


# def retrieve_relevant_clause(vendor_name: str, query: str, top_k: int = 1):
#     contracts = _load_contracts()
#     contract = next((c for c in contracts if c["vendor_name"] == vendor_name), None)
#     if contract is None or not contract.get("clauses"):
#         return None
def retrieve_relevant_clause(contract: dict, query: str, top_k: int = 1):
    if not contract or not contract.get("clauses"):
        return None
    model = _get_model()
    clause_embeddings = model.encode(contract["clauses"])
    query_embedding = model.encode(query)

    scored = [
        (contract["clauses"][i], _cosine_sim(query_embedding, clause_embeddings[i]))
        for i in range(len(contract["clauses"]))
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


if __name__ == "__main__":
    # quick manual smoke test
    results = retrieve_relevant_clause(
        "Sharma Digital Solutions Pvt. Ltd.",
        "what happens if there's an emergency weekend callout not in the normal scope?",
    )
    for clause, score in results:
        print(f"[{score:.3f}] {clause}")
