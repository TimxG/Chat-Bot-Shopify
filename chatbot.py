import os
from google import genai

# 1. Setup the Client
# Replace 'YOUR_API_KEY' with your actual key or set it as an environment variable
client = genai.Client(api_key="AIzaSyALLLE3sKPl7OCukAXnyWderF_qengUcR4")

def start_chat():
    # 2. Initialize a chat session
    # Using 'gemini-2.0-flash' for speed and efficiency
    chat = client.chats.create(model="gemini-3-flash-preview")
    
    print("AI: Hello! How can I help you today? (Type 'exit' to quit)")
    
    while True:
        user_input = input("You: ")
        
        if user_input.lower() in ["exit", "quit", "bye"]:
            print("AI: Goodbye!")
            break
            
        try:
            # 3. Send message and get response
            response = chat.send_message(user_input)
            print(f"AI: {response.text}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    start_chat()