from fastapi import FastAPI

from apps.api.settings import get_settings

app = FastAPI(title="Rebound", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "mode": settings.rebound_mode.value,
        "executor": settings.executor.value,
        "surface": settings.surface.value,
        "chaos": settings.chaos_profile.value,
    }
