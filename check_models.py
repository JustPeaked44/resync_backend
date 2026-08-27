import os
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

api_key = os.getenv("GEMINI_API_KEY")
url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"

print(f"Fetching models with key: {api_key[:10]}...")
response = requests.get(url)

if response.status_code == 200:
    data = response.json()
    print("\nAVAILABLE MODELS:")
    for model in data.get("models", []):
        name = model.get("name")
        methods = model.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            print(f"- {name}")
else:
    print(f"\n❌ ERROR {response.status_code}: {response.text}")
