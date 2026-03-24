"""
Minimal FastAPI application — placeholder for issue #2 (FastAPI scaffold).
Only the /health endpoint is defined here; everything else comes in issue #2.
"""
from fastapi import FastAPI

app = FastAPI(title="Photo Platform API")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
