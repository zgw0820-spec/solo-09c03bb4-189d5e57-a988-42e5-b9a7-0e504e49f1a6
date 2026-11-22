"""FastAPI 入口：提交多套环境配置、查询逐项校验结论与指令消耗。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .scheduler import Scheduler
from .wasm_runner import ValidatorEngine

STATIC_DIR = Path(__file__).with_name("static")


class EnvConfigIn(BaseModel):
    env_name: str = Field(..., min_length=1, max_length=64)
    conn_limit: int = Field(..., ge=0, le=10**12)
    timeout_ms: int = Field(..., ge=0, le=10**12)

    @field_validator("env_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("环境名称不能为空")
        return v


class BatchIn(BaseModel):
    per_item_fuel: int = Field(..., gt=0, description="单项指令额度（燃料）")
    total_budget: int = Field(..., gt=0, description="整批指令预算（燃料）")
    configs: list[EnvConfigIn] = Field(..., min_length=1)


def _serialize_item(item) -> dict:
    return {
        "id": item.id,
        "env_name": item.env_name,
        "conn_limit": item.conn_limit,
        "timeout_ms": item.timeout_ms,
        "fuel_quota": item.fuel_quota,
        "status": item.status.value,
        "verdict": item.verdict,
        "verdict_code": item.verdict_code,
        "consumed": item.consumed,
        "reason": item.reason,
    }


def _serialize_batch(batch) -> dict:
    return {
        "id": batch.id,
        "total_budget": batch.total_budget,
        "remaining_budget": batch.remaining_budget,
        "reserved": batch.reserved,
        "settled_consumed": batch.settled_consumed,
        "done": batch.done,
        "max_parallel": 2,
        "items": [_serialize_item(i) for i in batch.items],
        "history": batch.history,
        # 本进程实例池累计数据：实际新建、复用、淘汰等
        "instances": app.state.validator.stats(),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 同一校验器只编译一次，随进程生命周期持有
    app.state.validator = ValidatorEngine()
    app.state.scheduler = Scheduler(app.state.validator)
    await app.state.scheduler.start()
    yield
    await app.state.scheduler.shutdown()


app = FastAPI(title="代理换版环境配置 Wasm 校验服务", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/batches", status_code=201)
async def create_batch(payload: BatchIn) -> dict:
    scheduler: Scheduler = app.state.scheduler
    try:
        batch_id = scheduler.submit_batch(
            items_spec=[c.model_dump() for c in payload.configs],
            per_item_fuel=payload.per_item_fuel,
            total_budget=payload.total_budget,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_batch(scheduler.get_batch(batch_id))


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str) -> dict:
    try:
        batch = app.state.scheduler.get_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc
    return _serialize_batch(batch)


@app.get("/api/instances")
async def get_instances() -> dict:
    """本进程 Store/Instance 实例池累计统计（新建、复用、淘汰、在用/空闲）。"""
    return app.state.validator.stats()
