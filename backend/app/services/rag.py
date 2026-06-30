"""
Sara knowledge-book RAG (BM25)
==============================
Parses the K&K Legal Associates PDF once, splits it into overlapping
chunks, and serves the top-K most relevant chunks for any query via a
pure-Python BM25 ranker. No external embedding model, no GPU, no cloud
round-trip — sub-millisecond retrieval per query.

Why BM25 over dense embeddings here?
  * The knowledge book is small (single PDF, dozens of pages). BM25
    works extremely well on small corpora and has no cold-start cost.
  * Deterministic ranking → easier to debug ("why did Sara give that
    answer?").
  * Zero extra dependencies on Cloud Run (no torch / sentence-
    transformers binary that bloats the container by 1+ GB).

The retrieved chunks are stuffed into the LLM prompt so Sara's free-
form answers stay grounded in the knowledge book.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
from collections import Counter
from typing import List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Tokenisation
# --------------------------------------------------------------------------- #

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']+")

# Very small stop-word list — enough to declutter BM25 scoring without
# discarding legal-domain content.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "he", "in", "is", "it", "its", "of", "on", "or", "that",
    "the", "to", "was", "were", "will", "with", "this", "those",
    "these", "we", "you", "your", "i", "me", "my", "our", "us", "if",
    "then", "than", "but", "not", "no", "do", "does", "did", "have",
    "had", "can", "could", "should", "would", "may", "might", "so",
})


def _tokenize(text: str) -> List[str]:
    return [t.lower() for t in _WORD_RE.findall(text) if t.lower() not in _STOPWORDS]


# --------------------------------------------------------------------------- #
#  Chunking
# --------------------------------------------------------------------------- #

def _chunk_text(
    pages: List[str],
    target_words: int = 220,
    overlap_words: int = 60,
) -> List[str]:
    """Split a list of page-strings into overlapping windows.

    Overlap is essential because legal definitions often straddle page
    or paragraph boundaries — without overlap, BM25 might miss a chunk
    where half the keywords landed on each side of a cut.
    """
    chunks: List[str] = []
    words: List[str] = []
    for page in pages:
        page_words = [w for w in re.split(r"\s+", page or "") if w]
        words.extend(page_words)
    if not words:
        return chunks
    step = max(1, target_words - overlap_words)
    for start in range(0, len(words), step):
        window = words[start : start + target_words]
        if not window:
            break
        chunk = " ".join(window).strip()
        if chunk:
            chunks.append(chunk)
        if start + target_words >= len(words):
            break
    return chunks


# --------------------------------------------------------------------------- #
#  BM25 ranker
# --------------------------------------------------------------------------- #

class BM25:
    """Classic BM25 (Okapi). Built once over a fixed corpus."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: List[List[str]] = []
        self._doc_lens: List[int] = []
        self._df: Counter[str] = Counter()
        self._idf: dict[str, float] = {}
        self._avg_len: float = 0.0

    def fit(self, docs: List[List[str]]) -> None:
        self._docs = docs
        self._doc_lens = [len(d) for d in docs]
        self._avg_len = sum(self._doc_lens) / max(1, len(docs))
        self._df.clear()
        for doc in docs:
            for term in set(doc):
                self._df[term] += 1
        n = max(1, len(docs))
        # +1 smoothing to keep idf positive even for very common terms.
        self._idf = {
            term: math.log(1 + (n - df + 0.5) / (df + 0.5))
            for term, df in self._df.items()
        }

    def score(self, query: List[str], idx: int) -> float:
        if not self._docs or idx >= len(self._docs):
            return 0.0
        doc = self._docs[idx]
        if not doc:
            return 0.0
        tf = Counter(doc)
        dl = self._doc_lens[idx]
        score = 0.0
        for term in query:
            if term not in self._idf:
                continue
            f = tf.get(term, 0)
            if f == 0:
                continue
            num = f * (self.k1 + 1)
            den = f + self.k1 * (1 - self.b + self.b * (dl / max(1.0, self._avg_len)))
            score += self._idf[term] * (num / den)
        return score

    def search(self, query: List[str], k: int = 4) -> List[Tuple[int, float]]:
        if not self._docs:
            return []
        scored = [(i, self.score(query, i)) for i in range(len(self._docs))]
        scored = [(i, s) for i, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]


# --------------------------------------------------------------------------- #
#  KnowledgeBook — lazy-loaded singleton
# --------------------------------------------------------------------------- #

class KnowledgeBook:
    def __init__(self, pdf_path: str = "", raw_text: Optional[str] = None):
        self.pdf_path = pdf_path
        self._raw_text = raw_text
        self._chunks: List[str] = []
        self._tokenized: List[List[str]] = []
        self._bm25 = BM25()
        self._lock = threading.Lock()
        self._ready = False
        self._load_error: Optional[str] = None

    def _build(self, pages: List[str]) -> None:
        self._chunks = _chunk_text(pages)
        self._tokenized = [_tokenize(c) for c in self._chunks]
        self._bm25.fit(self._tokenized)
        self._ready = True

    def _ensure_loaded(self) -> None:
        if self._ready or self._load_error:
            return
        with self._lock:
            if self._ready or self._load_error:
                return
            # Source 1: raw text (a tenant's uploaded book, stored in the DB).
            if self._raw_text is not None:
                self._build([self._raw_text])
                logger.info("KnowledgeBook ready (text): %d chunks", len(self._chunks))
                return
            # Source 2: a PDF file path (platform default / env override).
            try:
                from pypdf import PdfReader
            except ImportError:
                self._load_error = "pypdf-not-installed"
                logger.warning("KnowledgeBook: pypdf missing — RAG disabled")
                return
            if not os.path.exists(self.pdf_path):
                self._load_error = f"missing:{self.pdf_path}"
                logger.warning("KnowledgeBook: %s not found — RAG disabled",
                               self.pdf_path)
                return
            try:
                reader = PdfReader(self.pdf_path)
                pages = [(p.extract_text() or "") for p in reader.pages]
                self._build(pages)
                logger.info(
                    "KnowledgeBook ready: %d pages → %d chunks from %s",
                    len(pages), len(self._chunks),
                    os.path.basename(self.pdf_path),
                )
            except Exception as exc:  # noqa: BLE001
                self._load_error = f"parse-error:{exc}"
                logger.exception("KnowledgeBook load failed")

    # ------------------------------------------------------------------ #
    def is_ready(self) -> bool:
        self._ensure_loaded()
        return self._ready

    def search(self, query: str, k: int = 4) -> List[str]:
        """Return the top-K chunks most relevant to ``query``.

        Returns an empty list if the book hasn't loaded (no PDF, pypdf
        missing) so callers can degrade gracefully.
        """
        self._ensure_loaded()
        if not self._ready:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        hits = self._bm25.search(tokens, k=k)
        return [self._chunks[i] for i, _s in hits]


_books: dict[str, "KnowledgeBook"] = {}
_text_books: dict[str, "KnowledgeBook"] = {}
_book_lock = threading.Lock()


def get_knowledge_book(pdf_path: Optional[str] = None) -> KnowledgeBook:
    """Return the (lazily-built) BM25 index for ``pdf_path``.

    Each distinct PDF path gets its own cached :class:`KnowledgeBook` so
    every tenant's knowledge book is parsed once and served independently.
    ``pdf_path=None`` falls back to the platform default
    (``settings.sara_knowledge_pdf``).
    """
    path = (pdf_path or settings.sara_knowledge_pdf or "").strip()
    book = _books.get(path)
    if book is not None:
        return book
    with _book_lock:
        book = _books.get(path)
        if book is None:
            book = KnowledgeBook(path)
            _books[path] = book
        return book


def get_knowledge_book_from_text(text: str) -> KnowledgeBook:
    """Cached BM25 index for a tenant's uploaded knowledge book (DB text).

    Keyed by a content hash so re-uploading the same book reuses the index
    and editing it transparently rebuilds a fresh one.
    """
    key = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    book = _text_books.get(key)
    if book is not None:
        return book
    with _book_lock:
        book = _text_books.get(key)
        if book is None:
            book = KnowledgeBook(raw_text=text)
            _text_books[key] = book
        return book


def retrieve(
    query: str,
    k: int = 4,
    pdf_path: Optional[str] = None,
    text: Optional[str] = None,
) -> List[str]:
    """Top-K knowledge chunks for ``query``.

    Prefers a tenant's uploaded knowledge book (``text``, DB-backed) and
    falls back to a PDF file path (the platform default when ``None``).
    """
    if text and text.strip():
        return get_knowledge_book_from_text(text).search(query, k=k)
    return get_knowledge_book(pdf_path).search(query, k=k)
