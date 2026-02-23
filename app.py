from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import os
from user import create_user, get_user_by_email
from flask_bcrypt import Bcrypt
from auth import generate_token, verify_token
from db import db
from memory import init_db, save_message, get_context_for_ai, auto_manage_memory
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime
import anthropic
import json
import re
import urllib.request
import urllib.parse

# Load environment variables
load_dotenv()

# Initialize DB
init_db()

app = Flask(__name__)
CORS(app, origins=[
    "https://tim-135344450.myshopify.com",
    "https://*.myshopify.com",
    "http://127.0.0.1:5000",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "null"
])

# Collections
products_collection = db["products"]
inventory_collection = db["inventory"]
shops_collection = db["shops"]
chat_sessions = db["chat_sessions"]

# Anthropic setup
api_key = os.getenv("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=api_key)

# Create text index for product search (run once)
try:
    products_collection.create_index([("name", "text"), ("description", "text")])
except:
    pass


# ===========================
# DISTANCE CALCULATION
# ===========================
def calculate_distance(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return 6371 * c


def get_nearest_shops(user_lat, user_lon, limit=5):
    all_shops = list(shops_collection.find())
    shops_with_distance = []
    for shop in all_shops:
        if "location" in shop and "coordinates" in shop["location"]:
            shop_lon = shop["location"]["coordinates"][0]
            shop_lat = shop["location"]["coordinates"][1]
            distance = calculate_distance(user_lat, user_lon, shop_lat, shop_lon)
            shops_with_distance.append({**shop, "distance_km": round(distance, 2)})
    shops_with_distance.sort(key=lambda x: x["distance_km"])
    return shops_with_distance[:limit]


# ===========================
# FOOD INTENT DETECTION
# ===========================
FOOD_KEYWORDS = [
    "hungry", "starving", "food", "eat", "eating", "restaurant",
    "cafe", "coffee", "lunch", "dinner", "breakfast", "snack",
    "meal", "bite to eat", "something to eat", "drink", "thirsty",
    "craving", "famished", "peckish", "where can i eat", "places to eat"
]

def detect_food_intent(text):
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in FOOD_KEYWORDS)


# ===========================
# NEARBY RESTAURANT SEARCH (Overpass API)
# ===========================
def get_nearby_restaurants(lat, lon, radius_meters=1500, limit=3):
    overpass_query = f"""
    [out:json][timeout:10];
    (
      node["amenity"="restaurant"](around:{radius_meters},{lat},{lon});
      node["amenity"="cafe"](around:{radius_meters},{lat},{lon});
      node["amenity"="fast_food"](around:{radius_meters},{lat},{lon});
    );
    out body;
    """

    url = "https://overpass-api.de/api/interpreter"
    encoded_query = urllib.parse.urlencode({"data": overpass_query}).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=encoded_query, method="POST")
        req.add_header("User-Agent", "SpaCeylonChatbot/1.0")

        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        places = []
        for element in data.get("elements", []):
            tags = element.get("tags", {})
            place_lat = element.get("lat")
            place_lon = element.get("lon")

            if not place_lat or not place_lon:
                continue

            name = tags.get("name")
            if not name:
                continue

            distance_km = calculate_distance(lat, lon, place_lat, place_lon)

            amenity_type = tags.get("amenity", "restaurant").replace("_", " ").title()
            cuisine = tags.get("cuisine", "").replace(";", ", ").title()
            opening_hours = tags.get("opening_hours", "Hours not listed")
            phone = tags.get("phone", tags.get("contact:phone", ""))
            address_parts = [
                tags.get("addr:housenumber", ""),
                tags.get("addr:street", ""),
                tags.get("addr:city", "")
            ]
            address = " ".join(part for part in address_parts if part).strip() or "Address not listed"

            places.append({
                "name": name,
                "type": amenity_type,
                "cuisine": cuisine,
                "address": address,
                "opening_hours": opening_hours,
                "phone": phone,
                "distance_km": round(distance_km, 2),
                "lat": place_lat,
                "lon": place_lon
            })

        places.sort(key=lambda x: x["distance_km"])
        return places[:limit]

    except Exception as e:
        print(f"Overpass API error: {e}")
        return []


def format_restaurant_context(restaurants):
    if not restaurants:
        return "\nNo nearby restaurants or cafes found in your area."

    lines = []
    for r in restaurants:
        line = f"• **{r['name']}** ({r['type']})"
        if r['cuisine']:
            line += f" — Cuisine: {r['cuisine']}"
        line += f"\n  📍 {r['address']}"
        line += f"\n  🕐 {r['opening_hours']}"
        line += f"\n  📏 {r['distance_km']} km away"
        if r['phone']:
            line += f"\n  📞 {r['phone']}"
        lines.append(line)

    return "\nNEARBY RESTAURANTS & CAFES:\n" + "\n\n".join(lines)


# ===========================
# SYSTEM PROMPT (uses AI-detected sentiment)
# ===========================
def get_system_prompt(sentiment):
    base_prompt = """You are Spa Ceylon's Ayurvedic Wellness Assistant - a knowledgeable and friendly expert in Ayurvedic beauty and wellness products.

CORE PERSONALITY:
- Professional yet warm and approachable
- Knowledgeable about Ayurvedic principles and Sri Lankan heritage
- Focus on helping customers find products that suit their needs
- Natural, conversational tone

RESPONSE GUIDELINES:
1. Keep responses SHORT and HELPFUL (2-4 sentences)
2. Be ACCURATE - only use information provided in the context
3. If product details are given, include: name, price, location, and stock
4. If information is missing, simply say "I don't have that information"
5. NEVER make up prices, locations, or product details
6. Don't be overly promotional - be genuinely helpful

FORMATTING RULES:
- Use **bold** for product names and prices (e.g., **Jasmine Mist** - **LKR 1,200**)
- Use bullet points (- ) when listing multiple products
- Keep responses concise but well-structured
- Break information into natural paragraphs for readability

CONVERSATION STYLE:
- Remember user's name if they provide it
- Reference previous conversation naturally
- Ask clarifying questions if needed
- Suggest relevant products when appropriate

FOOD & RESTAURANT SUGGESTIONS:
- If the user mentions being hungry, wanting food, or looking for a cafe/restaurant, use the nearby restaurant information provided to suggest options.
- Be friendly and helpful, mention the distance so they know how far each place is.
- Keep it brief — list the options clearly and let them choose.
"""
    
    if not sentiment or sentiment.get("mood") == "neutral":
        return base_prompt + "\n\nUSER MOOD: Neutral. Maintain a warm, professional tone."
    
    mood = sentiment.get("mood", "neutral")
    about = sentiment.get("about")
    intensity = sentiment.get("intensity", "moderate")
    
    if mood == "positive":
        return base_prompt + "\n\nUSER MOOD: The customer is happy! Match their positive energy with enthusiasm."
    
    elif mood == "negative":
        if about == "price":
            return base_prompt + f"\n\nUSER MOOD: Customer is {intensity}ly concerned about price. Emphasize value, suggest affordable alternatives, or explain benefits that justify the cost."
        elif about == "stock":
            return base_prompt + f"\n\nUSER MOOD: Customer is {intensity}ly frustrated about availability. Acknowledge the inconvenience, check other locations, or suggest similar in-stock products."
        elif about == "product":
            return base_prompt + f"\n\nUSER MOOD: Customer is {intensity}ly disappointed with a product. Be empathetic, ask what went wrong, suggest alternatives or solutions."
        else:
            return base_prompt + "\n\nUSER MOOD: Customer seems disappointed. Be extra empathetic and focus on solutions."
    
    elif mood == "angry":
        if about == "service":
            return base_prompt + f"\n\nUSER MOOD: Customer is {intensity}ly angry about service. Stay calm, apologize sincerely, acknowledge their frustration, and focus on immediate resolution."
        elif about == "price":
            return base_prompt + f"\n\nUSER MOOD: Customer is {intensity}ly upset about pricing. Stay professional, validate their concern, explain value, or suggest budget-friendly options."
        else:
            return base_prompt + "\n\nUSER MOOD: Customer is upset. Stay calm, acknowledge frustration, apologize if appropriate, focus on resolution."
    
    return base_prompt + "\n\nUSER MOOD: Maintain a warm, professional tone."


# ===========================
# STEP 1: AI ENTITY EXTRACTION (includes sentiment)
# ===========================
def extract_entities_with_ai(user_message, conversation_history=""):
    extraction_prompt = f"""Extract purchasing intent, product details, AND emotional sentiment from this customer message.

Conversation history (for context):
{conversation_history if conversation_history else "No prior conversation."}

Customer message: "{user_message}"

Return a JSON object with exactly these keys:
- "product": product name or keyword (string or null) 
- "location": city or shop name mentioned (string or null)
- "price_max": maximum price as integer in LKR (integer or null)
- "gender": "Men", "Women", or null (infer from context, e.g. "my wife" → "Women", "my husband" → "Men")
- "intent": one of "product_search", "stock_check", "price_check", "shop_info", "general"
- "sentiment": object with {{"mood": "positive/neutral/negative/angry", "about": "price/product/stock/location/service/general/null", "intensity": "low/moderate/high"}}

Sentiment detection rules:
- Identify the EMOTIONAL TONE (positive, neutral, negative, angry)
- Identify WHAT they're emotional about:
  * "too expensive" / "pricey" → about: "price"
  * "never in stock" / "always sold out" → about: "stock"  
  * "disappointed with quality" / "didn't work" → about: "product"
  * "far from me" / "inconvenient location" → about: "location"
  * "poor customer service" / "rude staff" → about: "service"
  * General complaints without a specific target → about: "general"
- Rate INTENSITY (low/moderate/high) based on word choice, punctuation, caps

General rules:
- Return ONLY the raw JSON object, no markdown, no explanation.
- If nothing is mentioned for a field, use null.
- Normalise product names to their likely official form (e.g. "jasmin mist" → "Jasmine Mist").
- Infer gender from pronouns or context clues when explicit gender words are absent.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=250,
            system="You are a precise entity and sentiment extractor. Return only valid JSON with no extra text.",
            messages=[{"role": "user", "content": extraction_prompt}]
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        entities = json.loads(raw)

        return {
            "product":   entities.get("product"),
            "location":  entities.get("location"),
            "price_max": entities.get("price_max"),
            "gender":    entities.get("gender"),
            "intent":    entities.get("intent", "general"),
            "sentiment": entities.get("sentiment", {"mood": "neutral", "about": None, "intensity": "low"})
        }

    except (json.JSONDecodeError, Exception) as e:
        print(f"Entity extraction error: {e}")
        return {
            "product": None,
            "location": None,
            "price_max": None,
            "gender": None,
            "intent": "general",
            "sentiment": {"mood": "neutral", "about": None, "intensity": "low"}
        }


# ===========================
# STEP 2: DETERMINISTIC DB QUERIES
# ===========================
def query_db_with_entities(entities):
    product_context = ""
    results = []
    raw_products = []

    product_query = {}

    if entities["product"]:
        product_query["$or"] = [
            {"name":        {"$regex": entities["product"], "$options": "i"}},
            {"description": {"$regex": entities["product"], "$options": "i"}}
        ]

    if entities["gender"]:
        g = entities["gender"].lower()
        if g in ["male", "men", "man"]:
            gender_pattern = "^Men$"
        elif g in ["female", "women", "woman", "girl"]:
            gender_pattern = "^Women$"
        else:
            gender_pattern = f"^{entities['gender']}$"
        product_query["gender"] = {"$regex": gender_pattern, "$options": "i"}

    matched_products = list(products_collection.find(product_query)) if product_query else []

    if not matched_products and entities["location"]:
        shop = shops_collection.find_one({
            "$or": [
                {"name":    {"$regex": entities["location"], "$options": "i"}},
                {"address": {"$regex": entities["location"], "$options": "i"}}
            ]
        })
        if shop:
            inventories = list(inventory_collection.find({"shopId": shop["shopId"]}))
            for inv in inventories:
                product = products_collection.find_one({"productId": inv["productId"]})
                if product:
                    results.append({
                        "name":     product["name"],
                        "shop":     shop["name"],
                        "address":  shop["address"],
                        "prices":   inv["prices"],
                        "stock":    inv["stock"]
                    })
                    seen_ids = {p["productId"] for p in raw_products}
                    if product["productId"] not in seen_ids:
                        raw_products.append(product)

    for product in matched_products:
        inv_query = {"productId": product["productId"]}

        if entities["location"]:
            shop = shops_collection.find_one({
                "$or": [
                    {"name":    {"$regex": entities["location"], "$options": "i"}},
                    {"address": {"$regex": entities["location"], "$options": "i"}}
                ]
            })
            if shop:
                inv_query["shopId"] = shop["shopId"]

        inventories = list(inventory_collection.find(inv_query))

        for inv in inventories:
            if entities["price_max"]:
                qualifying_sizes = {
                    size: price
                    for size, price in inv["prices"].items()
                    if price <= entities["price_max"]
                }
                if not qualifying_sizes:
                    continue
                inv_prices = qualifying_sizes
            else:
                inv_prices = inv["prices"]

            shop = shops_collection.find_one({"shopId": inv["shopId"]})
            if shop:
                results.append({
                    "name":     product["name"],
                    "shop":     shop["name"],
                    "address":  shop["address"],
                    "prices":   inv_prices,
                    "stock":    inv["stock"]
                })
                seen_ids = {p["productId"] for p in raw_products}
                if product["productId"] not in seen_ids:
                    raw_products.append(product)

    if results:
        lines = []
        for r in results[:8]:
            price_str = ", ".join([f"{k}: LKR {v}" for k, v in r["prices"].items()])
            stock_str = ", ".join([f"{k}: {v} units" for k, v in r["stock"].items()])
            lines.append(
                f"• {r['name']}\n"
                f"  Shop: {r['shop']} | {r['address']}\n"
                f"  Prices: {price_str}\n"
                f"  Stock:  {stock_str}"
            )
        product_context = "MATCHING PRODUCTS FROM DATABASE:\n" + "\n\n".join(lines)
    else:
        if any([entities["product"], entities["location"], entities["gender"], entities["price_max"]]):
            product_context = "DATABASE RESULT: No products found matching the customer's criteria."
        else:
            product_context = "No specific product query detected."

    return product_context, raw_products


def build_product_cards(raw_products):
    cards = []
    for product in raw_products[:2]:  # Max 2 cards
        inventories = list(inventory_collection.find({"productId": product["productId"]}))

        agg_prices = {}
        agg_stock = {}
        for inv in inventories:
            for size, price in inv["prices"].items():
                if size not in agg_prices:
                    agg_prices[size] = price
                else:
                    agg_prices[size] = min(agg_prices[size], price)
            for size, qty in inv["stock"].items():
                agg_stock[size] = agg_stock.get(size, 0) + qty

        def _sort_key(s):
            m = re.search(r"(\d+(?:\.\d+)?)", s)
            return float(m.group(1)) if m else 0

        sizes = sorted(agg_prices.keys(), key=_sort_key)
        size_options = [
            {
                "size":      sz,
                "price":     agg_prices[sz],
                "inStock":   agg_stock.get(sz, 0) > 0,
                "stockQty":  agg_stock.get(sz, 0)
            }
            for sz in sizes
        ]

        min_price = min(agg_prices.values()) if agg_prices else 0

        cards.append({
            "productId":   product["productId"],
            "name":        product["name"],
            "description": product.get("description", ""),
            "image":       product.get("image", ""),
            "price":       min_price,
            "rating":      product.get("rating", 4.5),
            "reviews":     product.get("reviews", 0),
            "badge":       product.get("badge", ""),
            "subCategory": product.get("subCategory", ""),
            "gender":      product.get("gender", "Unisex"),
            "sizeOptions": size_options
        })
    return cards


# ===========================
# MARKDOWN FORMATTER
# ===========================
def format_reply_for_display(text):
    # Bold: **text** → <strong>text</strong>
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    
    # Italic: *text* → <em>text</em>
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    
    # Bullet points: - item → <li>item</li>
    lines = text.split('\n')
    in_list = False
    formatted_lines = []
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('- '):
            if not in_list:
                formatted_lines.append('<ul class="chat-list">')
                in_list = True
            formatted_lines.append(f'<li>{stripped[2:]}</li>')
        else:
            if in_list:
                formatted_lines.append('</ul>')
                in_list = False
            if stripped:
                formatted_lines.append(f'<p class="chat-paragraph">{stripped}</p>')
    
    if in_list:
        formatted_lines.append('</ul>')
    
    return ''.join(formatted_lines)


# ===========================
# MAIN CHAT ENDPOINT
# ===========================
@app.route("/chat", methods=["POST"])
def chat_api():
    try:
        # Authentication
        auth_header = request.headers.get("Authorization")
        user_id = None
        is_guest = True

        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            try:
                payload = verify_token(token)
                user_id = payload["userId"]
                is_guest = False
            except Exception:
                pass

        data = request.json
        user_message = data.get("message", "").strip()

        if not user_message:
            return jsonify({"error": "Message is required"}), 400

        # Load memory
        memory_context = get_context_for_ai(user_id, include_summary=True) if not is_guest else "New conversation."

        # STEP 1: AI entity + sentiment extraction
        entities = extract_entities_with_ai(user_message, memory_context)
        print(f"[Entities extracted] {entities}")

        sentiment = entities.get("sentiment", {"mood": "neutral", "about": None, "intensity": "low"})

        # STEP 2: Use Shopify products if sent by widget, else fall back to MongoDB
        shopify_products = data.get("shopify_products_summary", [])

        product_context = ""
        raw_products = []
        product_cards = []
        product_cards_intro = None

        if shopify_products:
            keyword   = (entities.get("product") or "").lower()
            gender    = (entities.get("gender") or "").lower()
            price_max = entities.get("price_max")

            matched = []
            for p in shopify_products:
                name  = (p.get("name") or "").lower()
                tags  = " ".join(p.get("tags") or []).lower()
                ptype = (p.get("type") or "").lower()
                price = p.get("price", 0)
                if keyword and not any(keyword in f for f in [name, tags, ptype]):
                    continue
                if price_max and price > price_max:
                    continue
                if gender:
                    combined = name + " " + tags
                    if gender in ["men", "male", "man"] and not any(w in combined for w in ["men", "male", "man", "him", "his"]):
                        continue
                    if gender in ["women", "female", "woman"] and not any(w in combined for w in ["women", "female", "woman", "her", "ladies"]):
                        continue
                matched.append(p)

            display = matched if matched else (shopify_products if not keyword else [])

            if display:
                lines = []
                for p in display[:8]:
                    currency = p.get("currency", "LKR")
                    price    = p.get("price", 0)
                    tags_str = ", ".join(p.get("tags") or [])
                    line = "\u2022 " + p["name"] + "\n"
                    line += "  Type: " + p.get("type", "N/A") + "\n"
                    line += "  Price: " + currency + " " + "{:,.0f}".format(price) + "\n"
                    line += "  Tags: " + (tags_str or "N/A")
                    lines.append(line)
                product_context = "SHOPIFY PRODUCTS AVAILABLE IN STORE:\n" + "\n\n".join(lines)

                if entities.get("intent") in ("product_search", "stock_check", "price_check") or keyword:
                    for p in display[:2]:
                        product_cards.append({"productId": p.get("id"), "name": p.get("name")})
                    if product_cards:
                        intro_map = {
                            "product_search": "Here are some products from our store:",
                            "stock_check":    "Here's what we have available:",
                            "price_check":    "Here are some options within your budget:",
                        }
                        product_cards_intro = intro_map.get(entities.get("intent"), "Here are our recommendations:")
            else:
                product_context = "No Shopify products matched the customer's criteria."
        else:
            product_context, raw_products = query_db_with_entities(entities)
            if raw_products and entities.get("intent") in ("product_search", "stock_check", "price_check"):
                product_cards = build_product_cards(raw_products)
                if product_cards:
                    intro_map = {
                        "product_search": "Here are our most popular products:",
                        "stock_check":    "Here's what we have in stock:",
                        "price_check":    "Here are some options within your budget:"
                    }
                    product_cards_intro = intro_map.get(entities.get("intent"), "Here are our recommendations:")

        # STEP 3.5: Check for food intent and fetch nearby restaurants
        restaurant_context = ""
        if detect_food_intent(user_message):
            user_lat = data.get("latitude")
            user_lon = data.get("longitude")
            
            if user_lat and user_lon:
                restaurants = get_nearby_restaurants(user_lat, user_lon, radius_meters=1500, limit=3)
                restaurant_context = format_restaurant_context(restaurants)
            else:
                restaurant_context = "\nNote: User location not available for restaurant search."

        # STEP 4: Generate final response
        system_prompt = get_system_prompt(sentiment)

        user_prompt = f"""CONVERSATION HISTORY:
{memory_context}

CURRENT USER MESSAGE:
{user_message}

EXTRACTED INTENT: {entities['intent']}
DETECTED SENTIMENT: {sentiment['mood']} (about: {sentiment.get('about', 'general')}, intensity: {sentiment.get('intensity', 'moderate')})

PRODUCT INFORMATION (authoritative data from our database — do not invent anything outside this):
{product_context}

{restaurant_context}

Please provide a helpful, accurate response that matches the customer's emotional state. 
Include product names, prices, shop locations, and stock levels from the data above. 
If no products were found, say so politely and offer to help them find something else.

Remember to:
- Address their emotional concern if they expressed frustration
- Emphasize value if they're concerned about price
- Suggest alternatives if stock is an issue
- Be empathetic if they're disappointed with a product
- If restaurant information is provided, suggest the options naturally
"""

        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        reply_text = response.content[0].text

        # Persist to memory & session
        if not is_guest:
            save_message(user_id, "user", user_message)
            save_message(user_id, "assistant", reply_text)
            auto_manage_memory(user_id)

        now = datetime.utcnow()
        chat_sessions.insert_many([
            {"user_id": user_id, "role": "user",      "content": user_message, "timestamp": now},
            {"user_id": user_id, "role": "assistant",  "content": reply_text,   "timestamp": now}
        ])

        # Format reply
        formatted_reply = format_reply_for_display(reply_text)

        return jsonify({
            "reply":              formatted_reply,
            "sentiment":          sentiment,
            "entities":           entities,
            "product_cards":      product_cards,
            "product_cards_intro": product_cards_intro
        })

    except Exception as e:
        print(f"Chat API Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An error occurred processing your request"}), 500


# ===========================
# GET NEAREST SHOPS ENDPOINT
# ===========================
@app.route("/shops/nearest", methods=["POST"])
def get_nearest_shops_api():
    try:
        data = request.json
        if not data or "lat" not in data or "lon" not in data:
            return jsonify({"error": "Latitude and longitude required"}), 400

        nearest = get_nearest_shops(data["lat"], data["lon"], data.get("limit", 5))

        return jsonify({
            "shops": [
                {
                    "shopId":      shop["shopId"],
                    "name":        shop["name"],
                    "address":     shop["address"],
                    "distance_km": shop["distance_km"],
                    "location":    shop["location"]
                }
                for shop in nearest
            ]
        })
    except Exception as e:
        print(f"Nearest shops error: {e}")
        return jsonify({"error": "Failed to fetch nearest shops"}), 500


# ===========================
# AUTH ENDPOINTS
# ===========================
@app.route("/users/by-email", methods=["GET"])
def get_user_by_email_api():
    email = request.args.get("email")
    if not email:
        return jsonify({"error": "Email query parameter is required"}), 400
    user = get_user_by_email(email)
    if not user:
        return jsonify({"message": "User not found"}), 404
    user["_id"] = str(user["_id"])
    return jsonify(user), 200


@app.route("/auth/register", methods=["POST"])
def register():
    data = request.json
    if get_user_by_email(data["email"]):
        return jsonify({"error": "Email already exists"}), 400
    user_id = create_user({
        "first_name": data["firstName"],
        "last_name":  data["lastName"],
        "email":      data["email"],
        "phone":      data["phone"],
        "location":   data["location"],
        "password":   data["password"]
    })
    return jsonify({"message": "User registered", "userId": user_id}), 201


bcrypt = Bcrypt(app)


@app.route("/auth/login", methods=["POST"])
def login():
    data = request.json
    user = get_user_by_email(data["email"])
    if not user or not bcrypt.check_password_hash(user["password"], data["password"]):
        return jsonify({"error": "Invalid credentials"}), 401
    return jsonify({"token": generate_token(user["_id"]), "userId": str(user["_id"])})


# ===========================
# PRODUCT / INVENTORY / SHOP ENDPOINTS
# ===========================
@app.route("/api/products", methods=["POST"])
def add_product():
    data = request.json
    for field in ["productId", "name", "gender", "mainCategory", "subCategory", "description"]:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400
    products_collection.insert_one(data)
    return jsonify({"message": "Product added successfully"}), 201


@app.route("/api/inventory", methods=["POST"])
def add_inventory():
    data = request.json
    for field in ["productId", "shopId", "stock", "prices"]:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400
    inventory_collection.insert_one(data)
    return jsonify({"message": "Inventory added successfully"}), 201


@app.route("/api/shops", methods=["POST"])
def add_shop():
    data = request.json
    for field in ["shopId", "name", "location", "address"]:
        if field not in data:
            return jsonify({"error": f"{field} is required"}), 400
    shops_collection.insert_one(data)
    return jsonify({"message": "Shop added successfully"}), 201


@app.route("/api/products", methods=["GET"])
def get_all_products():
    try:
        query = {}
        category  = request.args.get("category")
        gender    = request.args.get("gender")
        max_price = request.args.get("max_price")
        search    = request.args.get("search")

        if category:
            query["subCategory"] = {"$regex": category, "$options": "i"}
        if gender:
            if gender.lower() in ["male", "men", "man"]:
                gender_pattern = "^Men$"
            elif gender.lower() in ["female", "women", "woman"]:
                gender_pattern = "^Women$"
            else:
                gender_pattern = f"^{gender}$"
            query["gender"] = {"$regex": gender_pattern, "$options": "i"}
        if search:
            query["$or"] = [
                {"name":        {"$regex": search, "$options": "i"}},
                {"description": {"$regex": search, "$options": "i"}}
            ]

        products = list(products_collection.find(query))
        enriched = []

        for product in products:
            inventories = list(inventory_collection.find({"productId": product["productId"]}))
            if not inventories:
                continue

            all_prices = [p for inv in inventories for p in inv["prices"].values()]
            min_price  = min(all_prices) if all_prices else 0

            if max_price and min_price > int(max_price):
                continue

            total_stock = sum(sum(inv["stock"].values()) for inv in inventories)

            enriched.append({
                "id":             str(product["_id"]),
                "productId":      product["productId"],
                "name":           product["name"],
                "description":    product["description"],
                "gender":         product.get("gender", "Unisex"),
                "mainCategory":   product.get("mainCategory", ""),
                "subCategory":    product.get("subCategory", ""),
                "price":          min_price,
                "image":          product.get("image", ""),
                "rating":         product.get("rating", 4.5),
                "reviews":        product.get("reviews", 0),
                "badge":          product.get("badge", ""),
                "totalStock":     total_stock,
                "availableShops": len(inventories)
            })

        return jsonify({"products": enriched, "count": len(enriched)})

    except Exception as e:
        print(f"Error fetching products: {e}")
        return jsonify({"error": "Failed to fetch products"}), 500


@app.route("/api/products/<product_id>", methods=["GET"])
def get_product_detail(product_id):
    try:
        product = products_collection.find_one({"productId": product_id})
        if not product:
            return jsonify({"error": "Product not found"}), 404

        inventories = list(inventory_collection.find({"productId": product_id}))
        shops_data  = []

        for inv in inventories:
            shop = shops_collection.find_one({"shopId": inv["shopId"]})
            if shop:
                shops_data.append({
                    "shopId":   shop["shopId"],
                    "shopName": shop["name"],
                    "address":  shop["address"],
                    "prices":   inv["prices"],
                    "stock":    inv["stock"],
                    "location": shop.get("location")
                })

        return jsonify({
            "id":           str(product["_id"]),
            "productId":    product["productId"],
            "name":         product["name"],
            "description":  product["description"],
            "gender":       product.get("gender", "Unisex"),
            "mainCategory": product.get("mainCategory", ""),
            "subCategory":  product.get("subCategory", ""),
            "image":        product.get("image", ""),
            "rating":       product.get("rating", 4.5),
            "reviews":      product.get("reviews", 0),
            "badge":        product.get("badge", ""),
            "shops":        shops_data
        })

    except Exception as e:
        print(f"Error fetching product detail: {e}")
        return jsonify({"error": "Failed to fetch product details"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
