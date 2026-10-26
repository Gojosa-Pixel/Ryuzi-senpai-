import os
from dotenv import load_dotenv

load_dotenv()


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


try:
    API_ID = int(required("API_ID"))
    OWNER_ID = int(required("OWNER_ID"))
    PORT = int(os.getenv("PORT", "8080"))
except ValueError as exc:
    raise RuntimeError("API_ID, OWNER_ID and PORT must be numeric values.") from exc

API_HASH = required("API_HASH")
BOT_TOKEN = required("BOT_TOKEN")
MONGO_URI = required("MONGO_URI")
