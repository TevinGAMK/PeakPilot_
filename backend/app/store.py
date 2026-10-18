"""Persistence layer.

Two interchangeable backends share one tiny document API:

* ``MongoStore``  – used when ``MONGODB_URI`` is set.
* ``FileStore``   – local demo fallback. Keeps every collection in memory and
  writes a JSON snapshot to disk after each mutation, so state survives
  navigation, refreshes and backend restarts during a demo session.

Queries are limited to equality filters so both backends behave identically.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable

COLLECTIONS = [
    "meta",
    "merchants",
    "products",
    "offers",
    "payment_methods",
    "shoppers",
    "intents",
    "quotes",
    "purchases",
    "events",
    "shipments",
    "claims",
    "remedies",
    "rules",
    "eval_runs",
    "policies",
    "shopify_orders",
    "shopify_events",
]


def _matches(doc: dict, flt: dict | None) -> bool:
    if not flt:
        return True
    for key, expected in flt.items():
        cur: Any = doc
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        if isinstance(expected, dict) and "$in" in expected:
            if cur not in expected["$in"]:
                return False
        elif cur != expected:
            return False
    return True


class BaseStore:
    id_field = "_id"
    mode = "base"

    def get(self, coll: str, doc_id: str) -> dict | None:  # pragma: no cover
        raise NotImplementedError

    def find(self, coll: str, flt: dict | None = None) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def put(self, coll: str, doc: dict) -> dict:  # pragma: no cover
        raise NotImplementedError

    def put_many(self, coll: str, docs: Iterable[dict]) -> None:
        for d in docs:
            self.put(coll, d)

    def delete(self, coll: str, doc_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def clear_all(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def find_one(self, coll: str, flt: dict) -> dict | None:
        res = self.find(coll, flt)
        return res[0] if res else None

    def snapshot(self) -> dict[str, list[dict]]:
        return {c: self.find(c) for c in COLLECTIONS}

    def load_snapshot(self, snap: dict[str, list[dict]]) -> None:
        self.clear_all()
        for c, docs in snap.items():
            self.put_many(c, docs)


class MemoryStore(BaseStore):
    """Pure in-memory store. Used for tests and sandboxed evaluation runs."""

    mode = "memory"

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {c: {} for c in COLLECTIONS}
        self._lock = threading.RLock()

    def get(self, coll, doc_id):
        with self._lock:
            d = self._data[coll].get(doc_id)
            return copy.deepcopy(d) if d else None

    def find(self, coll, flt=None):
        with self._lock:
            return [copy.deepcopy(d) for d in self._data[coll].values() if _matches(d, flt)]

    def put(self, coll, doc):
        with self._lock:
            self._data[coll][doc[self.id_field]] = copy.deepcopy(doc)
            self._persist()
            return doc

    def put_many(self, coll, docs):
        with self._lock:
            for d in docs:
                self._data[coll][d[self.id_field]] = copy.deepcopy(d)
            self._persist()

    def delete(self, coll, doc_id):
        with self._lock:
            self._data[coll].pop(doc_id, None)
            self._persist()

    def clear_all(self):
        with self._lock:
            self._data = {c: {} for c in COLLECTIONS}
            self._persist()

    def load_snapshot(self, snap):
        with self._lock:
            self._data = {c: {} for c in COLLECTIONS}
            for c, docs in snap.items():
                for d in docs:
                    self._data.setdefault(c, {})[d[self.id_field]] = copy.deepcopy(d)
            self._persist()

    def _persist(self) -> None:
        pass


class FileStore(MemoryStore):
    """In-memory store with a JSON snapshot on disk (local demo fallback)."""

    mode = "local-file"

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for c in COLLECTIONS:
                    self._data[c] = {d[self.id_field]: d for d in raw.get(c, [])}
            except (json.JSONDecodeError, KeyError):
                self._data = {c: {} for c in COLLECTIONS}

    def _persist(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({c: list(v.values()) for c, v in self._data.items()}, default=str), encoding="utf-8")
        tmp.replace(self.path)


class MongoStore(BaseStore):
    mode = "mongodb"

    def __init__(self, uri: str, db_name: str) -> None:
        from pymongo import MongoClient

        self.client = MongoClient(uri, serverSelectionTimeoutMS=4000)
        self.client.admin.command("ping")
        self.db = self.client[db_name]

    def get(self, coll, doc_id):
        return self.db[coll].find_one({"_id": doc_id})

    def find(self, coll, flt=None):
        return list(self.db[coll].find(flt or {}))

    def put(self, coll, doc):
        self.db[coll].replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return doc

    def delete(self, coll, doc_id):
        self.db[coll].delete_one({"_id": doc_id})

    def clear_all(self):
        for c in COLLECTIONS:
            self.db[c].delete_many({})


def create_store(live: bool = False) -> BaseStore:
    """Live mode fails closed: never fork real commerce state into a local snapshot."""
    uri = os.environ.get("MONGODB_URI", "").strip()
    if live and not uri:
        raise RuntimeError("PERKPILOT_MODE=live requires MONGODB_URI.")
    if uri:
        try:
            return MongoStore(uri, os.environ.get("MONGODB_DB", "perkpilot"))
        except Exception as exc:  # noqa: BLE001 - fall back loudly, never silently
            if live:
                raise
            print(f"[perkpilot] MongoDB unavailable ({exc}); using local file store fallback.")
    data_dir = Path(os.environ.get("PERKPILOT_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    return FileStore(data_dir / "demo_state.json")
