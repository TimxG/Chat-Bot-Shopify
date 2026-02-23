from pymongo import MongoClient
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

# MongoDB connection
MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)

# Database
db = client["chatbot_db"]

# Collections
chat_sessions = db["chat_sessions"]
users = db["users"]
user_summaries = db["user_summaries"]
user_preferences = db["user_preferences"]
mood_history = db["mood_history"]
product_interactions = db["product_interactions"]
unmet_product_requests = db["unmet_product_requests"]


def init_db():
    """
    Create indexes for better performance
    """
    try:
        chat_sessions.create_index([("user_id", 1), ("timestamp", -1)])
        user_preferences.create_index([("user_id", 1), ("type", 1)])
        mood_history.create_index([("user_id", 1), ("timestamp", -1)])
        user_summaries.create_index([("user_id", 1)])
        product_interactions.create_index([("user_id", 1), ("product_id", 1)])
        unmet_product_requests.create_index([("user_id", 1), ("timestamp", -1)])

        print("✅ Database indexes created")
    except Exception as e:
        print(f"⚠️ Index creation warning: {e}")

# ===========================
# BASIC MESSAGE FUNCTIONS
# ===========================
def save_message(user_id, role, content):
    chat_sessions.insert_one({
        "user_id": user_id,
        "role": role,
        "content": content,
        "timestamp": datetime.utcnow()
    })

    process_product_memory(user_id, role, content)
    
    # Auto-manage memory after saving
    auto_manage_memory(user_id)


def get_memory(user_id, limit=20):
    """
    Get recent chat history for a specific user
    Returns: List of (role, content) tuples
    """
    cursor = chat_sessions.find(
        {"user_id": user_id}
    ).sort("timestamp", -1).limit(limit)

    messages = [(doc["role"], doc["content"]) for doc in cursor]
    return messages[::-1]

# ===========================
# USER NAME HANDLING
# ===========================
def get_user_display_name(user_id):
    """
    Get the name to use for the user
    Priority:
    1. Preferred name (if user asked to be called something specific)
    2. First name from user record
    3. Full name from user record
    """
    from bson import ObjectId
    
    # Check for preferred name
    preferred = user_preferences.find_one({
        "user_id": user_id,
        "type": "preferred_name"
    })
    
    if preferred and preferred.get("value"):
        return preferred["value"]
    
    # Get from user record
    try:
        user = users.find_one({"_id": ObjectId(user_id)})
        if user:
            first_name = user.get("first_name", "")
            last_name = user.get("last_name", "")
            
            if first_name:
                return first_name
            elif first_name and last_name:
                return f"{first_name} {last_name}"
    except:
        pass
    
    return None

def set_preferred_name(user_id, preferred_name):
    """
    Save user's preferred name/nickname
    Use when user says "call me X" or "my name is X"
    """
    user_preferences.update_one(
        {"user_id": user_id, "type": "preferred_name"},
        {"$set": {"value": preferred_name, "updated_at": datetime.utcnow()}},
        upsert=True
    )
    print(f"✅ Set preferred name for user {user_id}: {preferred_name}")

def detect_preferred_name_request(message):
    """
    Detect if user is asking to be called a specific name
    Returns: preferred_name or None
    """
    message_lower = message.lower().strip()
    
    patterns = [
        "call me ",
        "you can call me ",
        "please call me ",
        "i prefer to be called ",
        "i go by ",
        "my friends call me ",
        "everyone calls me "
    ]
    
    for pattern in patterns:
        if pattern in message_lower:
            start_idx = message_lower.index(pattern) + len(pattern)
            remaining = message[start_idx:].strip()
            name = remaining.split()[0].strip(',.!?').capitalize()
            
            if len(name) >= 2 and name[0].isalpha():
                return name
    
    return None

# ===========================
# PRODUCT DETECTION & TRACKING
# ===========================
def is_product_request(text):
    t = text.lower()
    return (
        any(x in t for x in [
            "do you have", "available", "can i get", "looking for",
            "want", "need", "show me", "find", "search"
        ])
        or len(match_products_from_db(text)) > 0
    )


def match_products_from_db(text):
    text_lower = text.lower()
    matches = []

    for product in db.products.find({}, {"_id": 1, "name": 1, "status": 1}):
        if product["name"].lower() in text_lower:
            matches.append(product)

    return matches


def detect_product_intent(text):
    t = text.lower()

    if any(x in t for x in ["do you have", "available", "is there", "show me", "find"]):
        return "asked"
    if any(x in t for x in ["interested", "like this", "want this", "i'll take", "buy"]):
        return "interested"
    if any(x in t for x in ["out of stock", "not available"]):
        return "not_available"
    if any(x in t for x in ["don't like", "no thanks", "not interested"]):
        return "not_interested"

    return "mentioned"


def save_unmet_product_request(user_id, content):
    unmet_product_requests.update_one(
        {
            "user_id": user_id,
            "query": content.strip()[:150].lower()
        },
        {
            "$set": {"last_requested": datetime.utcnow()},
            "$inc": {"count": 1}
        },
        upsert=True
    )


def process_product_memory(user_id, role, content):
    if role != "user":
        return

    if not is_product_request(content):
        return

    matched_products = match_products_from_db(content)

    if matched_products:
        intent = detect_product_intent(content)
        for product in matched_products:
            product_interactions.update_one(
                {
                    "user_id": user_id,
                    "product_id": product["_id"],
                    "interaction": intent
                },
                {
                    "$set": {
                        "product_name": product["name"],
                        "last_updated": datetime.utcnow()
                    },
                    "$inc": {"count": 1}
                },
                upsert=True
            )
    else:
        save_unmet_product_request(user_id, content)

# ===========================
# ENHANCED PRODUCT SUMMARIZATION
# ===========================
def summarize_product_memory(user_id):
    """
    ENHANCED: Create comprehensive product interaction summary
    Returns detailed product history for AI context
    """
    # Get all product interactions
    products = list(product_interactions.find(
        {"user_id": user_id}
    ).sort("last_updated", -1))  # Most recent first
    
    # Get unmet requests
    unmet = list(unmet_product_requests.find(
        {"user_id": user_id}
    ).sort("last_requested", -1))

    # Categorize products by interaction type
    asked_products = []
    interested_products = []
    mentioned_products = []
    
    for p in products:
        product_data = {
            "product_id": str(p["product_id"]),
            "product_name": p["product_name"],
            "count": p.get("count", 1),
            "last_updated": p.get("last_updated")
        }
        
        interaction = p.get("interaction", "mentioned")
        if interaction == "asked":
            asked_products.append(product_data)
        elif interaction == "interested":
            interested_products.append(product_data)
        else:
            mentioned_products.append(product_data)

    return {
        "asked_about": asked_products,  # Products user specifically asked about
        "interested_in": interested_products,  # Products user showed interest in
        "mentioned": mentioned_products,  # Products casually mentioned
        "unmet_requests": [
            {
                "query": u["query"],
                "count": u.get("count", 1),
                "last_requested": u.get("last_requested")
            }
            for u in unmet
        ],
        "total_products": len(products),
        "total_unmet": len(unmet)
    }


# ===========================
# SMART SUMMARIZATION SYSTEM
# ===========================
def extract_key_info_from_messages(user_id, messages):
    """
    Extract key information from messages
    Returns structured data about the conversation
    """
    key_info = {
        "user_name": get_user_display_name(user_id),
        "preferred_name": None,
        "preferences": {},
        "concerns": [],
        "last_topics": []
    }
    
    # Check for preferred name request
    for role, content in messages:
        if role == "user":
            preferred = detect_preferred_name_request(content)
            if preferred:
                key_info["preferred_name"] = preferred
                set_preferred_name(user_id, preferred)
    
    # Extract concerns and topics
    for role, content in messages:
        content_lower = content.lower()
        
        if role == "user":
            # Extract concerns/needs
            concern_keywords = ["stress", "dry skin", "oily skin", "sensitive", 
                               "acne", "tired", "anxious", "sleep", "relaxation",
                               "pain", "headache", "insomnia", "wrinkles", "aging"]
            for concern in concern_keywords:
                if concern in content_lower:
                    if concern not in key_info["concerns"]:
                        key_info["concerns"].append(concern)
            
            # Track recent topics (last 5 user messages)
            if len(key_info["last_topics"]) < 5:
                topic = content[:50].strip()
                if topic:
                    key_info["last_topics"].append(topic)
    
    return key_info


def save_conversation_summary(user_id):
    """
    ENHANCED: Create detailed summary including comprehensive product history
    """
    # Get all recent messages
    all_messages = list(chat_sessions.find(
        {"user_id": user_id}
    ).sort("timestamp", 1))
    
    if len(all_messages) < 10:
        return None
    
    # Extract key information
    key_info = extract_key_info_from_messages(
        user_id,
        [(msg["role"], msg["content"]) for msg in all_messages]
    )

    # Get enhanced product summary
    product_summary = summarize_product_memory(user_id)
    
    # Create summary document
    summary = {
        "user_id": user_id,
        "user_name": key_info["user_name"],
        "preferred_name": key_info.get("preferred_name"),
        "concerns": key_info["concerns"],
        "recent_topics": key_info["last_topics"],
        "product_summary": product_summary,
        "total_messages": len(all_messages),
        "first_interaction": all_messages[0]["timestamp"],
        "last_interaction": all_messages[-1]["timestamp"],
        "updated_at": datetime.utcnow()
    }

    # Update or insert summary
    user_summaries.update_one(
        {"user_id": user_id},
        {"$set": summary},
        upsert=True
    )
    
    print(f"✅ Summary saved for user {user_id}")
    print(f"   - Total products: {product_summary['total_products']}")
    print(f"   - Asked about: {len(product_summary['asked_about'])}")
    print(f"   - Interested in: {len(product_summary['interested_in'])}")
    return summary


def get_user_summary(user_id):
    """
    Get the conversation summary for a user
    Returns: Summary document or None
    """
    return user_summaries.find_one({"user_id": user_id})


def get_context_for_ai(user_id, include_summary=True):
    """
    ENHANCED: Get context for AI with detailed product history
    """
    # Get user's display name
    display_name = get_user_display_name(user_id)
    
    # Get recent messages (last 20)
    recent_messages = get_memory(user_id, limit=20)
    
    # Get summary if available
    summary = get_user_summary(user_id) if include_summary else None
    
    context = ""

    # Add user info first
    if display_name:
        context += f"=== USER INFORMATION ===\n"
        context += f"User's name: {display_name}\n"
        
        # Check for preferred name
        pref = user_preferences.find_one({
            "user_id": user_id,
            "type": "preferred_name"
        })
        if pref and pref.get("value"):
            context += f"Preferred name: {pref['value']} (they asked to be called this)\n"
        
        context += "===================================\n\n"

    # ENHANCED PRODUCT HISTORY SECTION
    if summary and summary.get("product_summary"):
        ps = summary["product_summary"]
        
        context += "=== PRODUCT INTERACTION HISTORY ===\n"
        
        # Products user ASKED about
        if ps.get("asked_about"):
            context += "\n📋 Products User Asked About:\n"
            for p in ps["asked_about"][:10]:  # Show top 10
                context += f"   • {p['product_name']} (asked {p['count']} time(s))\n"
        
        # Products user showed INTEREST in
        if ps.get("interested_in"):
            context += "\n💚 Products User Was Interested In:\n"
            for p in ps["interested_in"][:10]:
                context += f"   • {p['product_name']} (interested {p['count']} time(s))\n"
        
        # Products casually MENTIONED
        if ps.get("mentioned"):
            context += "\n💬 Products User Mentioned:\n"
            for p in ps["mentioned"][:5]:
                context += f"   • {p['product_name']}\n"
        
        # Unmet requests
        if ps.get("unmet_requests"):
            context += "\n❌ Products User Searched For (Not Available):\n"
            for u in ps["unmet_requests"][:5]:
                context += f"   • \"{u['query']}\" (requested {u['count']} time(s))\n"
        
        context += f"\nTotal Products Interacted With: {ps.get('total_products', 0)}\n"
        context += "===================================\n\n"

    # Add conversation summary if exists
    if summary:
        context += "=== CONVERSATION SUMMARY ===\n"
        
        if summary.get("concerns"):
            context += f"User's concerns: {', '.join(summary['concerns'])}\n"
        
        if summary.get("recent_topics"):
            context += f"Recent topics: {', '.join(summary['recent_topics'][:3])}\n"
        
        context += f"Total messages in history: {summary.get('total_messages', 0)}\n"
        context += "===================================\n\n"
    
    # Add recent messages
    if recent_messages:
        context += "=== RECENT CONVERSATION (Last 20 Messages) ===\n"
        for role, content in recent_messages:
            if role == "user":
                context += f"User: {content}\n"
            else:
                context += f"You: {content}\n"
    
    return context

# ===========================
# CLEANUP & ARCHIVAL
# ===========================
def cleanup_old_messages(user_id, keep_recent=20):
    """
    Archive old messages for a user
    Keeps only the most recent N messages
    """
    total = chat_sessions.count_documents({"user_id": user_id})
    
    if total <= keep_recent:
        return 0
    
    cutoff_message = chat_sessions.find(
        {"user_id": user_id}
    ).sort("timestamp", -1).skip(keep_recent).limit(1)
    
    cutoff_message = list(cutoff_message)
    if not cutoff_message:
        return 0
    
    cutoff_time = cutoff_message[0]["timestamp"]
    
    result = chat_sessions.delete_many({
        "user_id": user_id,
        "timestamp": {"$lt": cutoff_time}
    })
    
    deleted = result.deleted_count
    print(f"🗑️ Cleaned up {deleted} old messages for user {user_id}")
    return deleted


def auto_manage_memory(user_id):
    """
    Automatically manage memory for a user
    Creates summary and cleans up old messages when threshold is reached
    """
    count = chat_sessions.count_documents({"user_id": user_id})
    
    # If more than 50 messages, summarize and cleanup
    if count >= 50:
        print(f"📝 Auto-managing memory for user {user_id} ({count} messages)")
        
        # Step 1: Save comprehensive summary
        save_conversation_summary(user_id)
        
        # Step 2: Cleanup old messages (keep last 20)
        cleanup_old_messages(user_id, keep_recent=20)
        
        return True
    
    return False

# ===========================
# USER PREFERENCES
# ===========================
def save_user_preference(user_id, preference_type, value):
    """Save user preferences for personalization"""
    user_preferences.update_one(
        {"user_id": user_id, "type": preference_type},
        {"$set": {"value": value, "updated_at": datetime.utcnow()}},
        upsert=True
    )

def get_user_preferences(user_id):
    """Get all user preferences"""
    preferences = user_preferences.find({"user_id": user_id})
    return {pref["type"]: pref["value"] for pref in preferences}

# ===========================
# MOOD TRACKING
# ===========================
def save_mood(user_id, mood_data):
    """Track user mood over time"""
    mood_history.insert_one({
        "user_id": user_id,
        "mood": mood_data.get("mood"),
        "polarity": mood_data.get("polarity"),
        "timestamp": datetime.utcnow()
    })

def get_mood_trend(user_id, days=7):
    """Get mood trend for last N days"""
    since = datetime.utcnow() - timedelta(days=days)
    moods = list(mood_history.find({
        "user_id": user_id,
        "timestamp": {"$gte": since}
    }))
    
    if not moods:
        return None
    
    total_polarity = sum([m.get("polarity", 0) for m in moods])
    avg_polarity = total_polarity / len(moods)
    
    return {
        "trend": "improving" if avg_polarity > 0.1 else "declining" if avg_polarity < -0.1 else "stable",
        "average_polarity": round(avg_polarity, 2),
        "message_count": len(moods)
    }

def clear_user_memory(user_id):
    """
    Clear all chat history for a user
    """
    result = chat_sessions.delete_many({"user_id": user_id})
    user_summaries.delete_one({"user_id": user_id})
    return result.deleted_count