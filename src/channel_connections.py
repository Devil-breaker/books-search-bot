"""MongoDB-backed registry of Telegram channels connected to the bot."""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo import MongoClient


class MongoChannelConnectionRepository:
    """Persist one current connection per Telegram channel."""

    def __init__(self, uri: str, database_name: str = "annie_db") -> None:
        self.client = MongoClient(
            uri,
            appname="AnnieSearchChannelConnections",
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
            waitQueueTimeoutMS=3000,
            maxPoolSize=5,
        )
        self.channels = self.client[database_name]["bot_channel_connections"]

    def connect(self, channel_id: int, channel_name: str, connected_by: int) -> bool:
        """Upsert by channel ID; return True only for a newly connected channel."""
        result = self.channels.update_one(
            {"_id": int(channel_id)},
            {"$set": {"name": channel_name, "updated_at": datetime.now(timezone.utc)},
             "$setOnInsert": {"connected_by": int(connected_by),
                              "created_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return result.upserted_id is not None

    def list_channels(self) -> list[dict[str, int | str]]:
        return [
            {
                "id": int(document["_id"]),
                "name": str(document.get("name") or "Telegram channel"),
            }
            for document in self.channels.find({}, {"name": 1}).sort("name", 1)
        ]

    def disconnect(self, channel_id: int) -> bool:
        channel_id = int(channel_id)
        removed = self.channels.delete_one({"_id": channel_id}).deleted_count > 0
        self.client[self.channels.database.name]["bot_channel_indexes"].delete_one(
            {"_id": channel_id}
        )
        return removed

    def close(self) -> None:
        self.client.close()
