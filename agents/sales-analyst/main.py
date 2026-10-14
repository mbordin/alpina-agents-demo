"""Entrypoint usato dalla build di Agent Manager: `python main.py` (porta 8000)."""

import os

import uvicorn

from app import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
