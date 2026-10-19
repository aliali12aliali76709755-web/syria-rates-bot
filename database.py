import os
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv(Path(__file__).parent / ".env")

mongo_url = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URI") or "mongodb://localhost:27017"
db_name = os.environ.get("DB_NAME") or "syria_rates_db"
client = AsyncIOMotorClient(mongo_url)
db = client[db_name]

async def get_settings():
    doc = await db.settings.find_one({"_id": "settings"})
    if not doc:
        doc = {
            "_id": "settings",
            "mandatory_channels": [],
            "recommended_links": [],
            "broadcast_channels": []
        }
        await db.settings.insert_one(doc)
    return doc

