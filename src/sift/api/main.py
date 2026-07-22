"""FastAPI application entry point.

Step 0 is a skeleton: a health check and nothing else. The `/recommend`
endpoint (dumb popularity retrieval) lands in build step 1, once the dataset
is loaded and the frozen temporal split is defined (see ARCHITECTURE.md).
"""

from fastapi import FastAPI

app = FastAPI(title="Sift", version="0.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
