"""Stage graph writes in memory, then flush on the fenced business transaction.

LangGraph may invoke saver callbacks on threads. SQLAlchemy sessions must never be
shared with those callbacks. Each invocation has its own isolated staging saver.
Only JSON primitives are accepted; no pickle or dynamic object deserialization.
"""

import json
import threading
from copy import deepcopy

from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple
from sqlalchemy import select

from app.db.models import ChatCheckpoint


class TransactionSaver(BaseCheckpointSaver):
    def __init__(self, db, run_id):
        super().__init__()
        self.run_id = run_id
        self.lock = threading.RLock()
        self.entries = {
            row.checkpoint_id: {
                "checkpoint": deepcopy(row.checkpoint),
                "meta": deepcopy(row.meta),
                "parent_id": row.parent_id,
                "writes": deepcopy(row.writes),
            }
            for row in db.scalars(select(ChatCheckpoint).where(ChatCheckpoint.run_id == run_id))
        }
        self.dirty = set()

    def config(self, checkpoint_id):
        return {
            "configurable": {
                "thread_id": self.run_id,
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            }
        }

    def get_tuple(self, config):
        with self.lock:
            if config["configurable"]["thread_id"] != self.run_id:
                raise ValueError("checkpoint thread mismatch")
            key = config["configurable"].get("checkpoint_id")
            if not key:
                key = max(self.entries, default=None)
            value = self.entries.get(key)
            if value is None:
                return None
            value = deepcopy(value)
            return CheckpointTuple(
                self.config(key),
                value["checkpoint"],
                value["meta"],
                self.config(value["parent_id"]) if value["parent_id"] else None,
                [(w[0], w[2], w[3]) for w in value["writes"]],
            )

    def put(self, config, checkpoint, metadata, new_versions):
        with self.lock:
            # Round-trip validates JSON-only state and detaches mutable references.
            self.entries[checkpoint["id"]] = json.loads(
                json.dumps(
                    {
                        "checkpoint": checkpoint,
                        "meta": metadata,
                        "parent_id": config["configurable"].get("checkpoint_id"),
                        "writes": [],
                    }
                )
            )
            self.dirty.add(checkpoint["id"])
            return self.config(checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        with self.lock:
            key = config["configurable"]["checkpoint_id"]
            entry = self.entries[key]
            existing = {(w[0], w[1]): w for w in entry["writes"]}
            for index, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, index)
                if index >= 0 and (task_id, index) in existing:
                    continue
                existing[(task_id, index)] = [task_id, index, channel, value]
            entry["writes"] = json.loads(json.dumps(list(existing.values())))
            self.dirty.add(key)

    def list(self, config, *, filter=None, before=None, limit=None):
        keys = sorted(self.entries, reverse=True)
        if before:
            keys = [k for k in keys if k < before["configurable"]["checkpoint_id"]]
        if filter:
            keys = [
                k
                for k in keys
                if all(self.entries[k]["meta"].get(a) == b for a, b in filter.items())
            ]
        for key in keys[:limit]:
            yield self.get_tuple(self.config(key))

    def flush(self, db):
        # Called on the owning thread, before the business transaction commits.
        for key in self.dirty:
            value = self.entries[key]
            row = db.get(ChatCheckpoint, (self.run_id, key))
            if row is None:
                row = ChatCheckpoint(run_id=self.run_id, checkpoint_id=key)
                db.add(row)
            row.checkpoint, row.meta = value["checkpoint"], value["meta"]
            row.parent_id, row.writes = value["parent_id"], value["writes"]
        db.flush()
