from db import users_collection
from flask_bcrypt import Bcrypt

bcrypt = Bcrypt()

def create_user(user_data):
    # Hash password
    hashed_pw = bcrypt.generate_password_hash(
        user_data["password"]
    ).decode("utf-8")

    user_data["password"] = hashed_pw
    result = users_collection.insert_one(user_data)
    return str(result.inserted_id)

def get_user_by_email(email):
    return users_collection.find_one({"email": email})