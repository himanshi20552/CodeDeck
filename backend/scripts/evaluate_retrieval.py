"""Run deterministic retrieval ranking metrics on the checked-in fixture corpus."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.services import repository_ai as rag


def rank_hash(chunks, query):
    query_vector = rag._hash_embedding(query)
    return sorted(range(len(chunks)), key=lambda i: rag._cosine(query_vector, rag._hash_embedding(chunks[i].content)), reverse=True)


def main():
    questions = json.loads((ROOT / "evals" / "retrieval_questions.json").read_text(encoding="utf-8"))
    corpus = ROOT / "evals" / "retrieval_corpus"
    with tempfile.TemporaryDirectory(prefix="codedeck-retrieval-eval-") as workspace:
        settings.workspace_dir = workspace
        metadata = rag.build_repository_index(corpus, 9001, "fixture://retrieval-eval")
        chunks = rag._all_chunks(9001)
        results = {name: [] for name in ("hash_baseline", "bm25", "semantic", "hybrid")}
        latency = {name: [] for name in results}
        for item in questions:
            query = item["question"]
            for name, ranker in (
                ("hash_baseline", rank_hash),
                ("bm25", rag._bm25_rank),
                ("semantic", rag._semantic_rank),
            ):
                started = time.perf_counter()
                ranked = ranker(chunks, query)
                latency[name].append((time.perf_counter() - started) * 1000)
                results[name].append([chunks[index].path for index in ranked[:3]])
            started = time.perf_counter()
            ranked_hybrid = rag.search_repository(9001, query, limit=3)
            latency["hybrid"].append((time.perf_counter() - started) * 1000)
            results["hybrid"].append([hit.path for hit in ranked_hybrid])

        answerable = [(i, item["expected_path"]) for i, item in enumerate(questions) if item["expected_path"]]
        report = {"index": metadata.model_dump(), "questions": len(questions), "answerable_questions": len(answerable), "systems": {}}
        for system, ranked_lists in results.items():
            reciprocal_ranks = []
            hits = 0
            for question_index, expected_path in answerable:
                paths = ranked_lists[question_index]
                if expected_path in paths:
                    hits += 1
                    reciprocal_ranks.append(1 / (paths.index(expected_path) + 1))
                else:
                    reciprocal_ranks.append(0)
            report["systems"][system] = {
                "recall_at_3": hits / len(answerable) if answerable else 0,
                "hit_rate_at_3": hits / len(answerable) if answerable else 0,
                "mrr_at_3": sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0,
                "mean_latency_ms": round(sum(latency[system]) / len(latency[system]), 3),
                "available": system != "semantic" or metadata.semantic_available,
            }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
