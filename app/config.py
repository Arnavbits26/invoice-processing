"""Central settings. Everything tunable lives here or in .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"
UPLOAD_DIR = ROOT / "data" / "uploads"
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "app.db"))

# Business rules
TOLERANCE_PCT = float(os.getenv("TOLERANCE_PCT", "2.0"))        # invoice may exceed PO balance by this much
ARITHMETIC_TOLERANCE = float(os.getenv("ARITHMETIC_TOLERANCE", "1.0"))  # rupees, for rounding in tax maths
VENDOR_NAME_MATCH = 88            # fuzzy score (0-100) to accept a vendor by name
PO_INFER_MIN_CONFIDENCE = 0.80    # below this an inferred PO is not trusted
PO_INFER_MIN_MARGIN = 0.15        # best candidate must beat the runner-up by this much
DUPLICATE_WINDOW_DAYS = 45        # fuzzy duplicate: same vendor and amount within this window
BUYER_GSTIN = os.getenv("BUYER_GSTIN", "29AABCM5678K1Z2")

# Extraction: "auto" uses Gemini when a key is present, else rules. "llm" or "rules" force one.
EXTRACTOR = os.getenv("EXTRACTOR", "auto")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Tried in order; the first that answers wins, and the rules extractor is the final fallback.
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-3.1-flash-lite,gemini-flash-latest,gemini-3.6-flash").split(",") if m.strip()]
GEMINI_MODEL = GEMINI_MODELS[0] if GEMINI_MODELS else ""
GEMINI_BUDGET = float(os.getenv("GEMINI_BUDGET", "60"))  # seconds across all models before giving up
GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low")  # low | medium | high
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "30"))

# Live demo pacing: seconds each stage "thinks" so a viewer can follow it. 0 for tests and fast mode.
STAGE_DELAY = float(os.getenv("STAGE_DELAY", "0.45"))
