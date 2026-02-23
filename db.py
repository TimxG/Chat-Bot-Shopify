from pymongo import MongoClient
import os
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

client = MongoClient(MONGO_URI)
db = client["chatbot_db"]

# Collections (auto-created)
users_collection = db["users"]
chat_sessions_collection = db["chat_sessions"]

products_collection = db["products"]
inventory_collection = db["inventory"]
shops_collection = db["shops"]
