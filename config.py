import os

# --- Paths ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

COOKIES_FILE = os.path.join(BASE_DIR, "cookies.json")
DATABASE_FILE = os.path.join(DATA_DIR, "daiko.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- File Upload Limits ---
MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", str(15 * 1024 * 1024)))  # 15MB
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "mov"}

# --- Admin (override via ADMIN_PASSWORD env var) ---
ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "0922c548475c5b7d1ef5e271d180561a7751c4f5444464e7"
)

# --- Rate Limits ---
MIN_POST_INTERVAL_SECONDS = int(os.environ.get("MIN_POST_INTERVAL", "900"))

# --- Flask ---
SECRET_KEY = os.environ.get(
    "SECRET_KEY",
    "ffafdf9cf2feb663e5bf45a0a09bc2939628df7309b8a854cd83c01bd7a41fba"
)
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))
