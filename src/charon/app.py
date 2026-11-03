"""charon: Meru's purge - clears what a retired app left after its retention"""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="charon")


@app.get("/heartbeat", response_class=PlainTextResponse)
def heartbeat() -> str:
    """Meru's liveness and readiness probes (nuwa-app's heartbeat.path) - add a /ready once there are dependencies."""
    return "ok"
