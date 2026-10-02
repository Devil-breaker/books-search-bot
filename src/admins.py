"""MongoDB-backed allowlist for bot users exempt from search restrictions."""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo import MongoClient


class MongoBotAdminRepository:
    """Store authorized Telegram user IDs separately from bookshelf data."""

    def __init__(self, uri: str, database_name: str = "annie_db") -> None:
        self.client = MongoClient(
            uri,
            appname="AnnieSearchBotAdmins",
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
            waitQueueTimeoutMS=3000,
            maxPoolSize=5,
        )
        self.admins = self.client[database_name]["bot_admins"]

    def list_user_ids(self) -> set[int]:
        return {int(document["_id"]) for document in self.admins.find({}, {"_id": 1})}

    def authorize(self, user_id: int, added_by: int) -> bool:
        result = self.admins.update_one(
            {"_id": int(user_id)},
            {"$setOnInsert": {
                "added_by": int(added_by),
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        return result.upserted_id is not None

    def unauthorize(self, user_id: int) -> bool:
        return self.admins.delete_one({"_id": int(user_id)}).deleted_count > 0

    def close(self) -> None:
        self.client.close()
