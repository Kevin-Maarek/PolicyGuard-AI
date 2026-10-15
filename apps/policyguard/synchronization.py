import hashlib
import json
import threading

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from apps.policyguard import providers
from apps.policyguard.sas import SASClient
from shared.logic import normalize_rule, render
from shared.storage import now, uid


def digest(data):
    return hashlib.sha256(
        (data if isinstance(data, str) else json.dumps(data, sort_keys=True)).encode()
    ).hexdigest()


def revision(data):
    return f"{data.get('majorRevision', 1)}.{data.get('minorRevision', 0)}"


class Synchronizer:
    def __init__(
        self, store, client_factory=SASClient, embedder=providers.embed, identity=providers.embedding_identity
    ):
        self.store, self.client_factory, self.embedder, self.identity = (
            store,
            client_factory,
            embedder,
            identity,
        )
        self.lock = threading.Lock()

    def run(self):
        if not self.lock.acquire(blocking=False):
            return {"status": "busy"}
        run_id = uid()
        summary = {
            "id": run_id,
            "started_at": now(),
            "status": "running",
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "deleted": 0,
            "embeddings_regenerated": 0,
            "embeddings_reused": 0,
            "items": [],
        }
        self.store.put("sas_sync_runs", run_id, summary)
        try:
            self._sync(summary)
            summary.update(status="completed", completed_at=now())
        except Exception as exc:
            summary.update(status="failed", completed_at=now(), error=f"{type(exc).__name__}: {exc}")
        finally:
            self.store.put("sas_sync_runs", run_id, summary)
            self.lock.release()
        return summary

    def _sync(self, summary):
        previous = {a["id"]: a for a in self.store.all("sas_indexed_assets")}
        identity = self.identity()
        assets, edges = [], []
        with self.client_factory() as sas:
            for path, kind in [("/decisions/flows", "decision"), ("/businessRules/ruleSets", "rule_set")]:
                for meta in sas.collection(path):
                    old = previous.get(meta["id"])
                    unchanged_meta = (
                        old
                        and old["revision"] == revision(meta)
                        and old["raw"].get("modifiedTimeStamp") == meta.get("modifiedTimeStamp")
                    )
                    raw = (
                        old["raw"] if unchanged_meta else sas.get(f"{path}/{meta['id']}", kind == "rule_set")
                    )
                    assets.append(self._asset(raw, kind, None, revision(raw), identity))
                    if kind == "rule_set":
                        for n, rule in enumerate(raw["rules"]):
                            assets.append(self._asset(rule, "rule", raw["id"], revision(raw), identity))
                            edges.append((raw["id"], rule["id"], n))
                    else:
                        for n, step in enumerate(raw["flow"]["steps"]):
                            if "ruleSet" in step:
                                edges.append((raw["id"], step["ruleSet"]["id"], n))
            # Read collection metadata twice to reject a repository changed during the sync.
            check = {
                m["id"]: (revision(m), m.get("modifiedTimeStamp"))
                for path in ("/decisions/flows", "/businessRules/ruleSets")
                for m in sas.collection(path)
            }
            for asset in assets:
                if asset["kind"] != "rule" and check.get(asset["id"]) != (
                    asset["revision"],
                    asset["raw"].get("modifiedTimeStamp"),
                ):
                    raise ValueError("SAS revisions changed during synchronization; retry")
        needs_embeddings = []
        for asset in assets:
            old = previous.get(asset["id"])
            state = (
                "created"
                if old is None or old["deleted"]
                else (
                    "unchanged"
                    if old["content_hash"] == asset["content_hash"] and old["revision"] == asset["revision"]
                    else "updated"
                )
            )
            summary[state] += 1
            reuse = old and old["embedding_model"] == identity and old["text_hash"] == asset["text_hash"]
            if reuse:
                asset["embedding"] = old["embedding"]
                summary["embeddings_reused"] += 1
            else:
                needs_embeddings.append(asset)
            summary["items"].append(
                {
                    "id": asset["id"],
                    "name": asset["raw"]["name"],
                    "kind": asset["kind"],
                    "status": state,
                    "embedding": "reused" if reuse else "regenerated",
                }
            )
        vectors = self.embedder([a["search_text"] for a in needs_embeddings])
        for asset, vector in zip(needs_embeddings, vectors, strict=True):
            asset["embedding"] = vector
            summary["embeddings_regenerated"] += 1
        live_ids = {a["id"] for a in assets}
        deleted_ids = [a["id"] for a in previous.values() if not a["deleted"] and a["id"] not in live_ids]
        summary["deleted"] = len(deleted_ids)
        summary["mode"] = "full" if not previous else "incremental"
        summary["bm25_version"] = digest(sorted((a["id"], a["text_hash"]) for a in assets))
        # Nothing in the index is changed until every remote fetch and embedding succeeds.
        with self.store.session() as s:
            table = self.store.table("sas_indexed_assets")
            for asset in assets:
                s.execute(
                    insert(table)
                    .values(**asset)
                    .on_conflict_do_update(index_elements=[table.c.id], set_=asset)
                )
                snapshot = {
                    "raw": asset["raw"],
                    "canonical": asset["canonical"],
                    "content_hash": asset["content_hash"],
                }
                rev_id = digest(asset["id"] + asset["revision"] + asset["content_hash"])
                s.execute(
                    insert(self.store.table("sas_asset_revisions"))
                    .values(id=rev_id, parent_id=asset["id"], revision=asset["revision"], data=snapshot)
                    .on_conflict_do_nothing()
                )
            if deleted_ids:
                s.execute(
                    table.update().where(table.c.id.in_(deleted_ids)).values(deleted=True, sync_time=now())
                )
            s.execute(delete(self.store.table("asset_links")))
            s.execute(delete(self.store.table("sas_asset_dependencies")))
            for parent, child, position in edges:
                if child not in live_ids:
                    raise ValueError("Decision references an absent rule set")
                s.execute(
                    insert(self.store.table("asset_links")).values(
                        parent_id=parent, child_id=child, position=position
                    )
                )
                s.execute(
                    insert(self.store.table("sas_asset_dependencies")).values(
                        id=digest(parent + child),
                        parent_id=parent,
                        data={"child_id": child, "position": position},
                    )
                )
            for i, item in enumerate(summary["items"]):
                s.execute(
                    insert(self.store.table("sas_sync_items")).values(
                        id=f"{summary['id']}-{i}", parent_id=summary["id"], data=item
                    )
                )

    def _asset(self, raw, kind, parent, rev, identity):
        canonical = None
        text = f"{raw['name'].replace('_', ' ')} Domestic retail money transfer fraud controls."
        description = raw.get("description", "").strip()
        # Demo boilerplate duplicates the rule name and adds no business search context.
        if description and not description.startswith("Synthetic domestic retail"):
            text += f" {description}"
        if kind == "rule":
            try:
                node, action = normalize_rule(raw)
                canonical = {"expression": node.model_dump(), "action": action, "status": "supported"}
                text += f" Conditions: {render(node)}. Action: {action}."
                if any("same_owner_verified" in c["expression"] for c in raw["conditions"]):
                    text += " New beneficiary additional verification excluding verified same account holder. Transfer amount threshold."
            except (ValueError, KeyError, StopIteration) as exc:
                canonical = {"status": "unsupported", "error": str(exc)}
                text += " Unsupported expression: " + " ".join(
                    c.get("expression", "") for c in raw.get("conditions", [])
                )
        return dict(
            id=raw["id"],
            kind=kind,
            parent_id=parent,
            revision=rev,
            raw=raw,
            canonical=canonical,
            search_text=text,
            text_hash=digest(text),
            content_hash=digest(raw),
            embedding_model=identity,
            deleted=False,
            sync_time=now(),
        )
