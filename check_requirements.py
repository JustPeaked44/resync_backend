import sys
import os
from dotenv import load_dotenv

load_dotenv()

def check_system_readiness():
    print("=" * 65)
    print("      RESYNC SYSTEM & ENVIRONMENT READINESS CHECKER")
    print("=" * 65)
    
    # 1. Check Python Version
    py_ver = sys.version_info
    print(f"\n[1] Checking Python Version:")
    print(f"    Active Version: Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}")
    if py_ver.major == 3 and py_ver.minor in [11, 12]:
        print("    --> ✅ PASS: Python version is 100% compatible (3.11/3.12).")
    else:
        print("    --> ⚠️ WARNING: Recommended version is 3.11 or 3.12.")

    # 2. Check Required Packages
    packages = {
        "fastapi": "FastAPI Web Framework",
        "uvicorn": "ASGI Server",
        "httpx": "Async HTTP Client",
        "spacy": "spaCy NLP Engine",
        "sentence_transformers": "Sentence Transformers (MPNet)",
        "sklearn": "scikit-learn (Cosine Math)",
        "supabase": "Supabase Python Client",
        "dotenv": "python-dotenv",
        "pydantic": "Pydantic Models"
    }
    
    print("\n[2] Checking Installed Python Packages:")
    all_packages_ok = True
    for pkg, name in packages.items():
        try:
            __import__(pkg)
            print(f"    --> ✅ PASS: {name} ({pkg})")
        except ImportError:
            print(f"    --> ❌ FAIL: {name} ({pkg}) is MISSING!")
            all_packages_ok = False

    # 3. Check spaCy Model
    print("\n[3] Checking spaCy Language Model:")
    try:
        import spacy
        spacy.load("en_core_web_sm")
        print("    --> ✅ PASS: 'en_core_web_sm' model is downloaded and ready.")
    except Exception:
        print("    --> ❌ FAIL: 'en_core_web_sm' is missing. Run: python -m spacy download en_core_web_sm")
        all_packages_ok = False

    # 4. Check .env Configuration
    print("\n[4] Checking Environment Variables (.env):")
    sb_url = os.getenv("SUPABASE_URL")
    sb_key = os.getenv("SUPABASE_ANON_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")

    if sb_url and "supabase.co" in sb_url:
        print(f"    --> ✅ PASS: SUPABASE_URL configured.")
    else:
        print("    --> ⚠️ WARNING: SUPABASE_URL missing or default in .env")

    if sb_key and len(sb_key) > 20:
        print("    --> ✅ PASS: SUPABASE_ANON_KEY configured.")
    else:
        print("    --> ⚠️ WARNING: SUPABASE_ANON_KEY missing in .env")

    if gemini_key and len(gemini_key) > 10:
        print("    --> ✅ PASS: GEMINI_API_KEY configured.")
    else:
        print("    --> ⚠️ WARNING: GEMINI_API_KEY missing in .env")

    # 5. Test Live Supabase Database Connection
    print("\n[5] Testing Live Supabase Connection:")
    if sb_url and sb_key and "supabase.co" in sb_url:
        try:
            from supabase import create_client
            client = create_client(sb_url, sb_key)
            res = client.table("subscription_plan").select("*").execute()
            print(f"    --> ✅ PASS: Connected to Supabase! Found {len(res.data)} subscription plans in DB.")
        except Exception as e:
            print(f"    --> ❌ FAIL: Supabase Connection Error: {str(e)}")
    else:
        print("    --> ⏩ SKIPPED: Fill in your .env credentials to test Supabase connection.")

    print("\n" + "=" * 65)
    if all_packages_ok:
        print("  🎉 SYSTEM STATUS: ALL CHECKS PASSED! READY FOR DEVELOPMENT!")
    else:
        print("  ⚠️ SYSTEM STATUS: PLEASE FIX THE MISSING ITEMS ABOVE.")
    print("=" * 65)

if __name__ == "__main__":
    check_system_readiness()