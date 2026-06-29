"""
MongoDB persistence layer for LLM proxy.
Stores session data, input/output token counts, timing info, and request history.
"""

import os
import time
from datetime import datetime, timezone
from bson import ObjectId
import motor.motor_asyncio


class SessionDB:
    """Async MongoDB connection wrapper with helper methods."""

    def __init__(self):
        mongo_uri = os.environ.get("MONGO_URI", "")
        mongo_db_name = os.environ.get("MONGO_DB", "radiacode")
        self.enabled = bool(mongo_uri)
        self.client = None
        self.db = None

        if not self.enabled:
            return

        try:
            self.client = motor.motor_asyncio.AsyncIOMotorClient(
                mongo_uri, serverSelectionTimeoutMS=3000
            )
            self.db = self.client[mongo_db_name]
            # Collections — indexes created async in ensure_connection()
            self.sessions = self.db.sessions
            self.requests = self.db.requests
            self.token_usage = self.db.token_usage
        except Exception as e:
            print(f"[warn] MongoDB connection failed: {e} — running in memory-only mode")
            self.enabled = False

    async def ensure_connection(self) -> None:
        """Test & warm up the DB connection, then create indexes."""
        if not self.enabled or not self.client:
            return
        try:
            await self.db.command("ping")
            # Create indexes for fast time-range queries
            await self.requests.create_index("timestamp", background=True)
            await self.requests.create_index("session_id", background=True)
            print("[db] MongoDB connected ✓")
        except Exception as e:
            print(f"[warn] MongoDB ping failed: {e}")
            self.enabled = False

    async def save_request(self, request_data: dict) -> None:
        """Persist a completed request to MongoDB."""
        if not self.enabled:
            return
        try:
            request_data["timestamp"] = datetime.now(timezone.utc)
            await self.requests.insert_one(request_data)
        except Exception as e:
            print(f"[db warn] Failed to save request: {e}")

    async def get_requests(self, limit: int = 200, days: int = 30,
                           sort_order: int = -1) -> list:
        """Fetch historical requests from the last N days."""
        if not self.enabled:
            return []
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            cursor = (
                self.requests.find({"timestamp": {"$gte": cutoff}})
                .sort("timestamp", sort_order)
                .limit(limit)
            )
            docs = await cursor.to_list(length=limit)
            for d in docs:
                d["_id"] = str(d["_id"])
            return docs
        except Exception as e:
            print(f"[db warn] Failed to fetch requests: {e}")
            return []

    async def get_token_usage_by_day(self, days: int = 30) -> list:
        """Aggregate total input + output tokens grouped by day."""
        if not self.enabled:
            return []
        try:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            pipeline = [
                {"$match": {"timestamp": {"$gte": cutoff}}},
                {
                    "$group": {
                        "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}},
                        "total_input_tokens": {"$sum": "$prompt_tokens"},
                        "total_output_tokens": {"$sum": "$completion_tokens"},
                        "total_total_tokens": {"$sum": {"$add": ["$prompt_tokens", "$completion_tokens"]}},
                        "request_count": {"$sum": 1},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
            docs = await self.requests.aggregate(pipeline).to_list(length=days)
            for d in docs:
                d["date"] = d["_id"]
                d.pop("_id", None)
            return docs
        except Exception as e:
            print(f"[db warn] Failed to aggregate daily usage: {e}")
            return []

    async def get_token_usage_by_hour(self, days: int = 7) -> list:
        """Aggregate total tokens grouped by hour bucket."""
        if not self.enabled:
            return []
        try:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            pipeline = [
                {"$match": {"timestamp": {"$gte": cutoff}}},
                {
                    "$group": {
                        "_id": {
                            "$dateToString": {"format": "%Y-%m-%dT%H:00", "date": "$timestamp"}
                        },
                        "total_tokens": {"$sum": {"$add": ["$prompt_tokens", "$completion_tokens"]}},
                        "input_tokens": {"$sum": "$prompt_tokens"},
                        "output_tokens": {"$sum": "$completion_tokens"},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
            docs = await self.requests.aggregate(pipeline).to_list(length=200)
            for d in docs:
                d["hour"] = d["_id"]
                d.pop("_id", None)
            return docs
        except Exception as e:
            print(f"[db warn] Failed to aggregate hourly usage: {e}")
            return []

    async def get_stats_summary(self, days: int = 30) -> dict:
        """Get summary statistics over the last N days."""
        if not self.enabled:
            return {}
        try:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            pipeline = [
                {"$match": {"timestamp": {"$gte": cutoff}}},
                {
                    "$group": {
                        "_id": None,
                        "total_requests": {"$sum": 1},
                        "total_input_tokens": {"$sum": "$prompt_tokens"},
                        "total_output_tokens": {"$sum": "$completion_tokens"},
                        "total_total_tokens": {"$sum": {"$add": ["$prompt_tokens", "$completion_tokens"]}},
                        "avg_prompt_tokens": {"$avg": "$prompt_tokens"},
                        "avg_completion_tokens": {"$avg": "$completion_tokens"},
                        "avg_duration": {"$avg": "$duration_secs"},
                        "error_count": {
                            "$sum": {"$cond": ["$has_error", 1, 0]}
                        },
                    }
                },
            ]
            result = await self.requests.aggregate(pipeline).to_list(length=1)
            if result:
                return result[0]
            return {}
        except Exception as e:
            print(f"[db warn] Failed to get stats summary: {e}")
            return {}

    async def get_cost_summary(self, days: int = 30) -> dict:
        """Aggregate token usage and compute equivalent cloud & local GPU costs."""
        if not self.enabled:
            return {}
        try:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            pipeline = [
                {"$match": {"timestamp": {"$gte": cutoff}}},
                {
                    "$group": {
                        "_id": None,
                        "total_requests": {"$sum": 1},
                        "total_input_tokens": {"$sum": "$prompt_tokens"},
                        "total_output_tokens": {"$sum": "$completion_tokens"},
                        "total_total_tokens": {"$sum": {"$add": ["$prompt_tokens", "$completion_tokens"]}},
                        "total_duration_secs": {"$sum": "$duration_secs"},
                    }
                },
            ]
            result = await self.requests.aggregate(pipeline).to_list(length=1)
            if result:
                return result[0]
            return {}
        except Exception as e:
            print(f"[db warn] Failed to get cost summary: {e}")
            return []

    async def get_cost_by_day(self, days: int = 30) -> list:
        """Aggregate daily token usage for cost-per-day chart."""
        if not self.enabled:
            return []
        try:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            pipeline = [
                {"$match": {"timestamp": {"$gte": cutoff}}},
                {
                    "$group": {
                        "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}},
                        "total_input_tokens": {"$sum": "$prompt_tokens"},
                        "total_output_tokens": {"$sum": "$completion_tokens"},
                        "total_total_tokens": {"$sum": {"$add": ["$prompt_tokens", "$completion_tokens"]}},
                        "total_duration_secs": {"$sum": "$duration_secs"},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
            docs = await self.requests.aggregate(pipeline).to_list(length=days)
            for d in docs:
                d["date"] = d["_id"]
                d.pop("_id", None)
            return docs
        except Exception as e:
            print(f"[db warn] Failed to get daily cost: {e}")
            return []

    async def close(self) -> None:
        if self.client:
            self.client.close()
