import logging
import time
import traceback
from uuid import uuid4
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi import Request
from fastapi.exceptions import RequestValidationError

from app.api import analysis, deployments, projects, system
from app.core.config import settings
from app.db.database import init_db
from app.services.observability import (
    langfuse_status,
    metrics_asgi_app,
    metrics_status,
    record_http_request,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("codedeck")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting CodeDeck")
    try:
        init_db()
        logger.info("Database schema ready")
    except Exception as exc:
        logger.error("Database initialization failed (%s); readiness will remain false", type(exc).__name__)
    try:
        from app.services.kubernetes import prepare_kubeconfig

        prepare_kubeconfig()
    except Exception:
        logger.warning("kubeconfig not prepared yet (kind may be missing)")
    yield


app = FastAPI(title="CodeDeck", version="1.0.0", lifespan=lifespan)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if not origins:
    origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials="*" not in origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(analysis.router)
app.include_router(deployments.router)
app.include_router(system.router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "status": "invalid_request",
            "message": "Request validation failed",
            "errors": [
                {"location": list(error.get("loc", ())), "message": error.get("msg", "Invalid value"), "type": error.get("type", "validation_error")}
                for error in exc.errors()
            ],
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    stack = "".join(traceback.format_tb(exc.__traceback__))[-4000:]
    logger.error("Unhandled request error id=%s type=%s stack=%s", request_id, type(exc).__name__, stack)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "Internal server error", "request_id": request_id},
    )


@app.middleware("http")
async def collect_http_metrics(request: Request, call_next):
    request.state.request_id = uuid4().hex
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        if request.url.path != "/metrics" and not request.url.path.startswith("/metrics/"):
            route = getattr(request.scope.get("route"), "path", "unmatched")
            record_http_request(request.method, route, 500, time.perf_counter() - started)
        raise
    if request.url.path != "/metrics" and not request.url.path.startswith("/metrics/"):
        route = getattr(request.scope.get("route"), "path", "unmatched")
        record_http_request(request.method, route, response.status_code, time.perf_counter() - started)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


metrics_app = metrics_asgi_app()
if metrics_app is not None:
    app.mount("/metrics", metrics_app, name="metrics")


@app.get("/")
def root():
    return JSONResponse(
        {
            "service": "codedeck",
            "status": "ok",
            "docs": "/docs",
            "health": "/health",
        }
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/health")
def health():
    return {"status": "ok", "service": "codedeck"}


@app.get("/health/live")
def health_live():
    return {"status": "ok", "service": "codedeck"}


@app.get("/health/ready")
def health_ready():
    from sqlalchemy import text
    from app.db.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db.rollback()
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable"})
    finally:
        db.close()
    return {"status": "ready", "database": "available"}


@app.get("/api/system/integrations")
def integration_status():
    return {
        "langfuse": langfuse_status(),
        "prometheus": metrics_status(),
    }
