"""市政道路桥梁养护管理平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.replay import ReplayConflict, ReplayError
from app.routers import ROUTERS
from app.store import store

app = FastAPI(title="市政道路桥梁养护管理平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.exception_handler(ReplayConflict)
def replay_conflict_handler(_: Request, exc: ReplayConflict) -> JSONResponse:
    """乐观锁/事件版本冲突：调用方必须重新拉取数据后再提交。"""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ReplayError)
def replay_error_handler(_: Request, exc: ReplayError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
