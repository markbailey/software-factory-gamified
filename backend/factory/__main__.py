"""Run with: uv run python -m factory   (from the backend folder)"""
import uvicorn

from .main import cfg

if __name__ == "__main__":
    uvicorn.run("factory.main:app", host=cfg.host, port=cfg.port, log_level="info")
