import os
from dotenv import load_dotenv

load_dotenv()

def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

API_ID = int(_required("API_ID"))
API_HASH = _required("API_HASH")
BOT_TOKEN = _required("BOT_TOKEN")
OWNER_ID = int(_required("OWNER_ID"))
MONGO_URI = _required("MONGO_URI")
BYPASS_API_URL = _required("BYPASS_API_URL")
PORT = int(os.getenv("PORT", "8080"))
