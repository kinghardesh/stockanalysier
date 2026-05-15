from fastapi import FastAPI

from app.dashboard.routes import router as dashboard_router
from app.routers import health

app = FastAPI(title="Trading Agent", version="0.4.0")
app.include_router(health.router)
app.include_router(dashboard_router)
