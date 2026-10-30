import sys
import os
from pathlib import Path

os.environ["SEMANTIC_EMBEDDINGS_ENABLED"] = "false"
os.environ["DATABASE_URL"] = "sqlite://"
for key in ("OPENAI_API_KEY", "GROK_API_KEY", "GROQ_API_KEY", "XAI_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ[key] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
