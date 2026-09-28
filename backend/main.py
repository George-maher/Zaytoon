import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.main import app
from config import config, configure_logging
import uvicorn

if __name__ == "__main__":
    # Installed before uvicorn so the per-attempt model records appear in the
    # console. Without it the root logger is WARNING-only and every successful
    # free-model call is invisible — failures looked fine and the happy path
    # was not recorded at all.
    configure_logging()
    uvicorn.run(
        "main:app",
        host=config.BACKEND_HOST,
        port=config.BACKEND_PORT,
        reload=False,
        log_level="info",
    )
