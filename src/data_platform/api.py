"""项目四（助贷对话数据中台）的 HTTP 接口。

按"数据运营"的实际工作顺序组织，前端可以照着一步步点：

    GET  /rules              规则透明化（脱敏规则 / 分类口径 / 三层脱敏）
    POST /ingest             跑一轮抽取清洗（dry_run 只出报告）
    GET  /pipeline           漏斗：原始 → 块 → 待审 → 已批准 → 导出 → 知识
    GET  /staging            待审队列
    POST /staging/{id}/review 审核：approve / reject / edit
    GET  /audits             审核流水（谁在什么时候批了什么）
    POST /export             导出四种格式（只导已批准数据）
    POST /knowledge/build    构建知识切片 + 文章
    GET  /knowledge/search   知识检索
    GET  /feed               预览"供给下游的语料"（项目三/七取数口径）

错误映射刻意区分：
    409 状态冲突（非法流转/未审批就导出）——调用方需要改动作，不是参数错
    503 数据源不可用                   ——可重试/需修配置
    422 参数不合法
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from data_platform.errors import DataPlatformError, InvalidTransition, ReviewRequired, SourceUnavailable

router = APIRouter(prefix="/data", tags=["data-platform"])

_VALID_ACTIONS = ("approve", "reject", "edit")
_VALID_FORMATS = ("sharegpt", "alpaca", "openai", "rag_csv")


def _ctx():
    from app_context import get_app_context

    return get_app_context()


def _handle(exc: Exception) -> HTTPException:
    """把领域异常翻成合适的 HTTP 状态码（别一律 400）。"""
    if isinstance(exc, (InvalidTransition, ReviewRequired)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SourceUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, DataPlatformError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


# =========================================================================== #
# 请求模型
# =========================================================================== #
class IngestRequest(BaseModel):
    dry_run: bool = Field(False, description="只出报告不写库")
    limit: int = Field(0, ge=0, description="最多抽取多少条消息（0=不限）")


class ReviewRequest(BaseModel):
    action: str = Field(..., description="approve | reject | edit")
    reviewer: str = Field("", description="审核人")
    comment: str = Field("", description="备注；驳回已批准数据时必填")
    messages: list[dict[str, Any]] | None = Field(
        None, description="action=edit 时提供编辑后的对话（[{role, content, ts}]）"
    )


class ExportRequest(BaseModel):
    format: str = Field(..., description="sharegpt | alpaca | openai | rag_csv")


class KnowledgeBuildRequest(BaseModel):
    limit: int = Field(0, ge=0, description="最多处理多少条已批准对话（0=全部）")
    incremental: bool = Field(False, description="增量构建：只处理上次构建之后新批准的对话（删除场景请走全量）")


# =========================================================================== #
# 规则与漏斗
# =========================================================================== #
@router.get("/rules", summary="脱敏规则 / 9 级分类口径 / 三层脱敏说明")
def rules() -> dict[str, Any]:
    return _ctx().data_platform.rules()


@router.get("/pipeline", summary="数据管线漏斗")
def pipeline() -> dict[str, Any]:
    return _ctx().data_platform.pipeline()


@router.get("/stats", summary="数据中台统计")
def stats() -> dict[str, Any]:
    return _ctx().data_platform.stats()


# =========================================================================== #
# 抽取
# =========================================================================== #
@router.post("/ingest", summary="抽取 + 清洗（dry_run 只出报告）")
def ingest(req: IngestRequest) -> dict[str, Any]:
    try:
        report = _ctx().data_platform.ingest(limit=req.limit, dry_run=req.dry_run)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {**report.model_dump(mode="json"), "text": report.to_text()}


# =========================================================================== #
# 暂存区与审核
# =========================================================================== #
@router.get("/staging", summary="待审/已审对话队列")
def list_staging(status: str = "", limit: int = 50) -> dict[str, Any]:
    rows = _ctx().data_platform.list_staging(status=status, limit=limit)
    return {
        "count": len(rows),
        "items": [s.model_dump(mode="json") for s in rows],
        "counts": _ctx().data_platform.stats()["staging"],
    }


@router.get("/staging/{staging_id}", summary="单条暂存详情")
def get_staging(staging_id: str) -> dict[str, Any]:
    staged = _ctx().data_platform.get_staging(staging_id)
    if staged is None:
        raise HTTPException(status_code=404, detail=f"暂存条目不存在：{staging_id}")
    return {
        "item": staged.model_dump(mode="json"),
        "audits": _ctx().data_platform.audits(staging_id=staging_id, limit=20),
    }


@router.post("/staging/{staging_id}/review", summary="审核：approve / reject / edit")
def review(staging_id: str, req: ReviewRequest) -> dict[str, Any]:
    if req.action not in _VALID_ACTIONS:
        raise HTTPException(status_code=422, detail=f"非法 action：{req.action}")
    try:
        staged = _ctx().data_platform.review(
            staging_id,
            req.action,  # type: ignore[arg-type]
            reviewer=req.reviewer,
            comment=req.comment,
            messages=req.messages,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"item": staged.model_dump(mode="json")}


@router.get("/audits", summary="审核流水（可审计）")
def audits(staging_id: str = "", limit: int = 50) -> dict[str, Any]:
    return {"items": _ctx().data_platform.audits(staging_id=staging_id, limit=limit)}


# =========================================================================== #
# 导出
# =========================================================================== #
@router.post("/export", summary="导出（sharegpt / alpaca / openai / rag_csv）")
def export(req: ExportRequest) -> dict[str, Any]:
    if req.format not in _VALID_FORMATS:
        raise HTTPException(status_code=422, detail=f"非法格式：{req.format}")
    try:
        manifest = _ctx().data_platform.export(req.format)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return manifest.model_dump(mode="json")


@router.get("/exports", summary="导出记录")
def exports(limit: int = 20) -> dict[str, Any]:
    rows = _ctx().data_platform.list_exports(limit=limit)
    return {"count": len(rows), "items": [m.model_dump(mode="json") for m in rows]}


# =========================================================================== #
# 知识产物
# =========================================================================== #
@router.post("/knowledge/build", summary="构建知识切片与结构化文章")
def build_knowledge(req: KnowledgeBuildRequest) -> dict[str, Any]:
    dp = _ctx().data_platform
    if req.incremental:
        report = dp.rebuild_knowledge(incremental=True, limit=req.limit)
    else:
        report = dp.build_knowledge(limit=req.limit)
    return {**report.model_dump(mode="json"), "text": report.to_text()}


@router.get("/knowledge/search", summary="知识检索")
def search_knowledge(q: str, top_k: int = 3) -> dict[str, Any]:
    hits = _ctx().data_platform.search_knowledge(q, top_k=top_k)
    return {"query": q, "count": len(hits), "hits": hits}


@router.get("/knowledge/articles", summary="结构化知识文章")
def articles(limit: int = 50) -> dict[str, Any]:
    store = _ctx().data_platform.store
    rows = store.list_articles(limit=limit)
    return {
        "count": len(rows),
        "items": [a.model_dump(mode="json") for a in rows],
        "extractor": store.article_extractor_counts(),
    }


# =========================================================================== #
# 下游供给
# =========================================================================== #
@router.get("/feed", summary="预览供给项目三/七的语料（只含已批准数据）")
def feed(limit: int = 20) -> dict[str, Any]:
    rows = _ctx().data_platform.feed_conversations(limit=limit)
    return {"count": len(rows), "items": rows, "note": "敏感对话不会出现在这里"}
