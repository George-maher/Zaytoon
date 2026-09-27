import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.main import app
from config import config
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.BACKEND_HOST,
        port=config.BACKEND_PORT,
        reload=False,
        log_level="info",
    )
