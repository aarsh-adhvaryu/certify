"""
BM25 Baseline Retriever — pure keyword matching, no embeddings.
Same interface as SearchKB so it can be swapped in for ablation comparison.

This is the BASELINE. It represents what every naive RAG system does:
query → keyword match → rank by term frequency.

It cannot handle:
- Hinglish ("mera PF nikalna hai" won't match "withdrawal")
- Paraphrases ("claim my provident fund" won't match "withdraw PF balance")
- Semantic similarity (no embeddings, no understanding)
"""

import json
import re
from rank_bm25 import BM25Okapi


class BM25Retriever:
    def __init__(self, store_path="../index/chunk_store.json"):
        print(f"Loading chunk store from {store_path} ...")
        with open(store_path, encoding="utf-8") as f:
            self.chunks = json.load(f)

        # Tokenize all documents for BM25
        self.tokenized = []
        for c in self.chunks:
            text = f"{c.get('title', '')} {c.get('content', '')}"
            tokens = self._tokenize(text)
            self.tokenized.append(tokens)

        self.bm25 = BM25Okapi(self.tokenized)
        print(f"BM25 ready: {len(self.chunks)} documents indexed.")

    def _tokenize(self, text: str) -> list[str]:
        """Simple whitespace + lowercase tokenizer."""
        text = text.lower()
        text = re.sub(r'[^\w\s]', ' ', text)  # remove punctuation
        tokens = text.split()
        # Remove very short tokens
        tokens = [t for t in tokens if len(t) > 1]
        return tokens

    def search(self, query: str, top_k: int = 4) -> list[dict]:
        """
        BM25 keyword search. Same return format as SearchKB.search().
        """
        query_tokens = self._tokenize(query)
        scores = self.bm25.get_scores(query_tokens)

        # Get top-k indices
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

        results = []
        for idx in top_indices:
            if scores[idx] <= 0:
                continue  # skip zero-score matches
            chunk = self.chunks[idx]
            results.append({
                "doc_id": chunk.get("doc_id", ""),
                "chunk_id": chunk.get("chunk_id", ""),
                "title": chunk.get("title", ""),
                "content": chunk.get("content", ""),
                "source_url": chunk.get("source_url", ""),
                "effective_date": chunk.get("effective_date"),
                "supersedes": chunk.get("supersedes"),
                "domain": chunk.get("domain", ""),
                "subdomain": chunk.get("subdomain", ""),
                "conditions": chunk.get("conditions", []),
                "forms": chunk.get("forms", []),
                "score": float(scores[idx]),
            })

        return results

    def format_for_prompt(self, results: list[dict]) -> str:
        """Same format as SearchKB.format_for_prompt()."""
        parts = []
        for i, r in enumerate(results):
            parts.append(
                f"[Source {i+1}] doc_id={r['doc_id']} | "
                f"date={r['effective_date']} | domain={r['domain']}\n"
                f"{r['content'][:1500]}"
            )
        return "\n\n---\n\n".join(parts)


# ── Test: compare BM25 vs FAISS on the same queries ───────────────────────────

if __name__ == "__main__":
    from search_kb import SearchKB

    bm25 = BM25Retriever()
    faiss_kb = SearchKB()

    test_queries = [
        # English — both should work
        "How to withdraw PF after leaving job?",
        # Hinglish — BM25 should fail, FAISS should work
        "mera PF nikalna hai job chhod diya",
        # Paraphrase — BM25 should fail
        "claim my provident fund settlement after unemployment",
        # Specific legal term
        "Gratuity eligibility after 5 years of service",
        # Payslip
        "Is my ESI deduction correct if gross salary is 20000?",
        # Tax
        "HRA exemption calculation under section 10(13A)",
        # Hinglish tax
        "87A rebate kitna h agar income exactly 7 lakh hai",
    ]

    print("=" * 80)
    print(f"{'QUERY':<45} | {'BM25 Top-1':<20} | {'FAISS Top-1':<20}")
    print("=" * 80)

    for q in test_queries:
        bm25_results = bm25.search(q, top_k=3)
        faiss_results = faiss_kb.search(q, top_k=3)

        bm25_top = bm25_results[0]["doc_id"] if bm25_results else "NO RESULTS"
        faiss_top = faiss_results[0]["doc_id"] if faiss_results else "NO RESULTS"

        match = "✓" if bm25_top == faiss_top else "✗"
        print(f"{q[:44]:<45} | {bm25_top:<20} | {faiss_top:<20} {match}")

    # Detailed comparison for one query
    print("\n" + "=" * 80)
    print("DETAILED: 'mera PF nikalna hai job chhod diya'")
    print("=" * 80)

    print("\n  BM25 results:")
    for r in bm25.search("mera PF nikalna hai job chhod diya", top_k=3):
        print(f"    {r['doc_id']} (score={r['score']:.2f}) — {r['content'][:80]}")

    print("\n  FAISS results:")
    for r in faiss_kb.search("mera PF nikalna hai job chhod diya", top_k=3):
        print(f"    {r['doc_id']} (score={r['score']:.2f}) — {r['content'][:80]}")
