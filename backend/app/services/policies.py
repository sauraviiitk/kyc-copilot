"""KYC policy knowledge base.

Documents live in `app/policies/documents.json` (single source of truth) and are
indexed either in ChromaDB (when installed) or by a deterministic local
retriever. Both return identical refs + scores so the agent and the evaluation
harness behave the same offline and in Docker.

ChromaDB uses a deterministic hashed bag-of-words embedding function, so no
model download is required and results are reproducible in CI.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

DOCUMENTS_PATH = Path(__file__).resolve().parents[1] / "policies" / "documents.json"
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be", "with",
    "must", "may", "should", "can", "this", "that", "it", "as", "at", "by", "if", "not", "no",
    "any", "all", "from", "when", "after", "before", "user", "kyc",
}
TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class PolicyDocument:
    ref: str
    title: str
    category: str
    version: str
    tags: tuple[str, ...]
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "title": self.title,
            "category": self.category,
            "version": self.version,
            "tags": list(self.tags),
            "content": self.content,
        }


@dataclass(frozen=True)
class PolicyHit:
    ref: str
    title: str
    content: str
    score: float
    category: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "title": self.title,
            "content": self.content,
            "score": round(self.score, 4),
            "category": self.category,
        }


def load_documents(path: Path | None = None) -> list[PolicyDocument]:
    raw = json.loads((path or DOCUMENTS_PATH).read_text(encoding="utf-8"))
    docs: list[PolicyDocument] = []
    for item in raw:
        docs.append(
            PolicyDocument(
                ref=item["ref"],
                title=item["title"],
                category=item.get("category", "general"),
                version=str(item.get("version", "1.0")),
                tags=tuple(item.get("tags", [])),
                content=item["content"].strip(),
            )
        )
    return docs


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall((text or "").lower()) if t not in STOPWORDS and len(t) > 2]


class LocalPolicyRetriever:
    """Deterministic BM25-style keyword retriever (no external dependency)."""

    backend = "local-bm25"

    def __init__(self, documents: list[PolicyDocument] | None = None):
        self.documents = documents or load_documents()

    @cached_property
    def _index(self) -> tuple[dict[str, list[str]], dict[str, float]]:
        token_sets = {doc.ref: tokenize(doc.title + " " + doc.content + " " + " ".join(doc.tags)) for doc in self.documents}
        lengths = {ref: len(tokens) for ref, tokens in token_sets.items()}
        avgdl = sum(lengths.values()) / max(len(lengths), 1)
        return token_sets, lengths | {"__avgdl__": avgdl}

    def search(self, query: str, k: int = 4) -> list[PolicyHit]:
        token_sets, meta = self._index
        avgdl = meta["__avgdl__"]
        q_tokens = tokenize(query)
        if not q_tokens:
            return [self._hit(doc, 0.0) for doc in self.documents[:k]]
        query_terms = set(q_tokens)
        n_docs = len(self.documents)
        df = {term: sum(1 for toks in token_sets.values() if term in toks) for term in query_terms}
        k1, b = 1.4, 0.75
        scored: list[tuple[float, PolicyDocument]] = []
        for doc in self.documents:
            toks = token_sets[doc.ref]
            tf_map: dict[str, int] = {}
            for token in toks:
                tf_map[token] = tf_map.get(token, 0) + 1
            score = 0.0
            for term in query_terms:
                tf = tf_map.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
                denom = tf + k1 * (1 - b + b * len(toks) / max(avgdl, 1))
                score += idf * (tf * (k1 + 1)) / denom
            if doc.category and doc.category in query.lower():
                score += 0.4
            for tag in doc.tags:
                if tag.lower() in query.lower():
                    score += 0.3
            scored.append((score, doc))
        scored.sort(key=lambda item: item[0], reverse=True)
        top = [item for item in scored if item[0] > 0][:k]
        if not top:
            top = scored[:k]
        return [self._hit(doc, score) for score, doc in top]

    @staticmethod
    def _hit(doc: PolicyDocument, score: float) -> PolicyHit:
        return PolicyHit(ref=doc.ref, title=doc.title, content=doc.content, score=score, category=doc.category)


class _HashingEmbeddingFunction:
    """Deterministic 256-dim hashed bag-of-words embedding (Chroma compatible)."""

    DIM = 256

    def __init__(self) -> None:
        import numpy as np

        self._np = np

    def name(self) -> str:  # chroma >=0.5 interface
        return "kyc-hashing-bow"

    def __call__(self, input):  # noqa: A002 - chroma signature
        import hashlib

        np = self._np
        vectors = []
        for text in input:
            vec = np.zeros(self.DIM, dtype="float32")
            for token in tokenize(text):
                digest = hashlib.md5(token.encode()).digest()
                idx = int.from_bytes(digest[:4], "big") % self.DIM
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vec[idx] += sign
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec /= norm
            vectors.append(vec.tolist())
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self(texts)

    def embed_query(self, text: str) -> list[float]:
        return self([text])[0]


class ChromaPolicyRetriever:
    backend = "chromadb"

    def __init__(self, documents: list[PolicyDocument] | None = None):
        import chromadb

        self.documents = documents or load_documents()
        self._client = chromadb.PersistentClient(path=str(settings.chroma_path))
        self._embedding = _HashingEmbeddingFunction()
        self._collection = self._client.get_or_create_collection(
            name="kyc_policies",
            embedding_function=self._embedding,
            metadata={"hnsw:space": "cosine"},
        )
        if self._collection.count() != len(self.documents):
            self._collection.upsert(
                ids=[doc.ref for doc in self.documents],
                documents=[doc.title + "\n" + doc.content for doc in self.documents],
                metadatas=[
                    {"ref": doc.ref, "title": doc.title, "category": doc.category, "version": doc.version}
                    for doc in self.documents
                ],
            )
        self._by_ref = {doc.ref: doc for doc in self.documents}

    def search(self, query: str, k: int = 4) -> list[PolicyHit]:
        result = self._collection.query(query_texts=[query], n_results=min(k, len(self.documents)))
        hits: list[PolicyHit] = []
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        for idx, ref in enumerate(ids):
            doc = self._by_ref.get(ref)
            if not doc:
                continue
            distance = distances[idx] if idx < len(distances) else 1.0
            hits.append(
                PolicyHit(
                    ref=doc.ref,
                    title=doc.title,
                    content=doc.content,
                    score=max(0.0, 1.0 - float(distance)),
                    category=doc.category,
                )
            )
        return hits


_retriever: LocalPolicyRetriever | ChromaPolicyRetriever | None = None


def get_policy_retriever(force: str | None = None) -> LocalPolicyRetriever | ChromaPolicyRetriever:
    global _retriever
    mode = force or settings.policy_backend
    if _retriever is not None and force is None:
        return _retriever
    if mode in {"auto", "chroma"}:
        try:
            _retriever = ChromaPolicyRetriever()
            logger.info("policy.retriever.ready backend=chromadb")
            return _retriever
        except Exception as exc:  # pragma: no cover - depends on env
            if mode == "chroma":
                logger.error("policy.chroma.unavailable: %s", exc)
            else:
                logger.info("policy.chroma.skipped: %s", exc)
    _retriever = LocalPolicyRetriever()
    logger.info("policy.retriever.ready backend=%s", _retriever.backend)
    return _retriever


def reset_retriever() -> None:
    global _retriever
    _retriever = None


def search_policies(query: str, k: int = 4) -> list[PolicyHit]:
    return get_policy_retriever().search(query, k=k)
