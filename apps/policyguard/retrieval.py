import re

from rank_bm25 import BM25Okapi

from apps.policyguard.providers import embed, embedding_identity
from apps.policyguard.synchronization import digest


def tokens(text):
    return re.findall(r"[a-z0-9_]+", text.lower())


def rrf(rankings, constant=60):
    scores = {}
    for ranking in rankings:
        for rank, id in enumerate(ranking, 1):
            scores[id] = scores.get(id, 0) + 1 / (constant + rank)
    return sorted(scores, key=lambda id: (-scores[id], id)), scores


class Retriever:
    def __init__(self, store):
        self.store = store
        self.corpus_version = None
        self.bm25 = None
        self.ids = []

    def search(self, query, kind=None, k=8, active_only=True):
        assets = [a for a in self.store.all("sas_indexed_assets") if not a["deleted"]]
        links = self.store.all("asset_links")
        if active_only:
            # Separate unconnected authoring drafts from rules participating in an indexed decision.
            reachable = {a["id"] for a in assets if a["kind"] == "decision"}
            for _ in range(3):
                reachable.update(e["child_id"] for e in links if e["parent_id"] in reachable)
            assets = [a for a in assets if a["id"] in reachable]
        if not assets:
            return {"bm25": [], "vector": [], "fused": [], "dependencies": [], "corpus_version": None}
        version = digest(sorted((a["id"], a["text_hash"]) for a in assets))
        if version != self.corpus_version:
            self.bm25 = BM25Okapi([tokens(a["search_text"]) for a in assets])
            self.ids = [a["id"] for a in assets]
            self.corpus_version = version
        lexical_scores = self.bm25.get_scores(tokens(query))
        lexical_by_id = {id: float(score) for id, score in zip(self.ids, lexical_scores)}
        metadata = {a["id"]: a for a in assets}
        bm25 = sorted(
            [
                (id, float(score))
                for id, score in zip(self.ids, lexical_scores)
                if not kind or metadata[id]["kind"] == kind
            ],
            key=lambda x: (-x[1], x[0]),
        )[:k]
        query_vector = embed(
            ["Instruct: Retrieve relevant banking decision rules for this policy.\nQuery: " + query]
        )[0]
        identity = embedding_identity()
        table = self.store.table("sas_indexed_assets")
        distance = table.c.embedding.cosine_distance(query_vector)
        q = (
            table.select()
            .add_columns(distance.label("distance"))
            .where(table.c.deleted.is_(False), table.c.embedding_model == identity)
        )
        if kind:
            q = q.where(table.c.kind == kind)
        if active_only:
            q = q.where(table.c.id.in_(self.ids))
        with self.store.session() as s:
            vector = [
                (r["id"], float(1 - r["distance"]))
                for r in s.execute(q.order_by(distance).limit(k)).mappings()
            ]
        ranking, scores = rrf([[id for id, _ in bm25], [id for id, _ in vector]])
        vector_by_id = dict(vector)
        missing_vector_ids = set(ranking[:k]) - vector_by_id.keys()
        if missing_vector_ids:
            # A lexical hit can enter the fused results outside the vector top k.
            with self.store.session() as s:
                for row in s.execute(q.where(table.c.id.in_(missing_vector_ids))).mappings():
                    vector_by_id[row["id"]] = float(1 - row["distance"])
        fused = [
            {
                "id": id,
                "name": metadata[id]["raw"]["name"],
                "kind": metadata[id]["kind"],
                "score": scores[id],
                "bm25_score": lexical_by_id[id],
                "vector_score": vector_by_id.get(id),
                "revision": metadata[id]["revision"],
                "parent_id": metadata[id]["parent_id"],
            }
            for id in ranking[:k]
        ]
        selected = {f["id"] for f in fused}
        expanded = set(selected)
        for _ in range(3):
            for edge in links:
                if edge["child_id"] in expanded:
                    expanded.add(edge["parent_id"])
        return {
            "bm25": [{"id": id, "score": score} for id, score in bm25],
            "vector": [{"id": id, "score": score} for id, score in vector],
            "fused": fused,
            "dependencies": sorted(expanded - selected),
            "corpus_version": version,
        }
