"""人工审核闸门（"唯一可信数据源"的落地）。

数据中台最容易做错的地方：把"清洗"当成"可用"。
真实业务里，一条对话要不要进训练集/知识库，必须有人签字 ——
因为错误的样本会被模型学走，而且很难事后归因。

四件事：

**① 状态机只有三条边，且都留痕**
        pending  → approved   批准（可导出）
        pending  → rejected   驳回
        approved → rejected   撤销（必须写理由 —— 已进过下游的数据撤回，要有据可查）
   非法流转直接抛 `InvalidTransition`，不做"善意猜测"。

**② 幂等**
    · 重复提交同一个 block → 返回已存在的那条，不改状态
      （重跑清洗不能把已批准的数据打回 pending，否则下游导出会突然少数据）
    · 重复 approve → 不加审计行，不重复动作

**③ 编辑即重置**
    编辑产生新 revision，并且回到 pending —— 改过的内容必须重新审。
    同时**重新跑脱敏与分类**：人工编辑是最容易把手机号/价格又贴回去的环节。

**④ 数据资格与审核状态分离**
    `dataset_eligible=False` 的（敏感对话）即使被批准，也永远不进训练集。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from data_platform.domain import (
    CATEGORY_NAMES,
    ConversationBlock,
    RawMessage,
    ReviewRecord,
    StagedConversation,
)
from data_platform.errors import InvalidTransition, ReviewRequired
from data_platform.filters import ConversationFilter
from data_platform.masking import MaskingEngine
from data_platform.store import DataPlatformStore


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ReviewGate:
    """暂存区审核状态机。"""

    def __init__(
        self,
        store: DataPlatformStore,
        *,
        masker: MaskingEngine,
        conv_filter: ConversationFilter,
    ) -> None:
        self.store = store
        self.masker = masker
        self.filter = conv_filter

    # ------------------------------------------------------------------ #
    # 提交
    # ------------------------------------------------------------------ #
    def submit(self, block: ConversationBlock) -> tuple[StagedConversation, bool]:
        """把对话块送进待审队列。返回 (staged, 是否新建)。

        只有 `keepable` 的块才进来（system/media/spam 等直接丢弃），
        但 `sensitive` 也会进来 —— 主管必须看到投诉苗头，
        它的 dataset_eligible=False 保证它永远进不了训练集。
        """
        if not block.keepable:
            raise InvalidTransition(
                f"{block.block_id} 分类为 {block.category}（{CATEGORY_NAMES.get(block.category, '')}），"
                "不属于可用数据，不应进入暂存区"
            )
        staged = StagedConversation(
            staging_id="stg_" + block.block_id.removeprefix("blk_"),
            block_id=block.block_id,
            status="pending",
            revision=1,
            category=block.category,
            quality_score=block.quality_score,
            dataset_eligible=block.dataset_eligible,
            session_id=block.session_id,
            customer_id=block.customer_id,
            messages=block.messages,
            qa_pairs=block.qa_pairs,
            findings_count=len(block.findings),
            price_hits=block.price_hits,
            created_at=_now(),
            updated_at=_now(),
        )
        created = self.store.submit_staged(staged)
        if not created:
            existing = self.store.get_staged(staged.staging_id) or staged
            return existing, False
        self._audit(staged, action="submit", from_status="", to_status="pending",
                    reviewer="系统", comment="清洗后进入待审队列", revision=1)
        return staged, True

    # ------------------------------------------------------------------ #
    # 审核动作
    # ------------------------------------------------------------------ #
    def approve(self, staging_id: str, *, reviewer: str, comment: str = "") -> StagedConversation:
        current = self._require(staging_id)
        if current.status == "approved":
            return current  # 幂等：重复批准不产生新的审计行
        if current.status == "rejected":
            raise InvalidTransition(
                f"{staging_id} 已被驳回，不能直接批准；请先 edit 修改后重新提交审核"
            )
        updated = current.model_copy(update={
            "status": "approved",
            "reviewer": reviewer or "未署名",
            "comment": comment,
            "decided_at": _now(),
            "updated_at": _now(),
        })
        self.store.update_staged(updated)
        self._audit(updated, action="approve", from_status=current.status,
                    to_status="approved", reviewer=reviewer, comment=comment,
                    revision=updated.revision)
        return updated

    def reject(self, staging_id: str, *, reviewer: str, comment: str = "") -> StagedConversation:
        current = self._require(staging_id)
        if current.status == "rejected":
            return current  # 幂等
        if current.status == "approved" and not comment.strip():
            # 撤销已批准的数据必须留理由：这批数据可能已经进了模型训练/知识库
            raise InvalidTransition(f"{staging_id} 已批准，撤销必须填写理由（comment）")
        updated = current.model_copy(update={
            "status": "rejected",
            "reviewer": reviewer or "未署名",
            "comment": comment,
            "decided_at": _now(),
            "updated_at": _now(),
        })
        self.store.update_staged(updated)
        self._audit(updated, action="reject", from_status=current.status,
                    to_status="rejected", reviewer=reviewer, comment=comment,
                    revision=updated.revision)
        return updated

    def edit(
        self,
        staging_id: str,
        *,
        reviewer: str,
        messages: list[RawMessage],
        comment: str = "",
    ) -> StagedConversation:
        """人工编辑消息内容 → 新 revision + 回到 pending。

        重新跑脱敏与分类是**硬要求**：人工编辑是最后一道可能把
        手机号/价格写回去的环节，不能信任"人不会贴错"。
        """
        current = self._require(staging_id)
        if not messages:
            raise InvalidTransition("编辑后的对话不能为空")

        # 写层脱敏（第二层）：即使人工把号码贴回来，这里也会拦掉
        masked, findings, price_hits = self.masker.mask_messages(messages, layer="write")
        rebuilt = self.filter.build_block(
            block_id=current.block_id,
            session_id=current.session_id or "",
            customer_id=current.customer_id or "",
            customer_name="",
            messages=masked,
            started_at=masked[0].ts,
            ended_at=masked[-1].ts,
            price_hits=price_hits,
        )
        updated = current.model_copy(update={
            "status": "pending",
            "revision": current.revision + 1,
            "category": rebuilt.category,
            "quality_score": rebuilt.quality_score,
            "dataset_eligible": rebuilt.dataset_eligible,
            "messages": rebuilt.messages,
            "qa_pairs": rebuilt.qa_pairs,
            "findings_count": current.findings_count + len(findings),
            "price_hits": current.price_hits + price_hits,
            "reviewer": reviewer or "未署名",
            "comment": comment,
            "decided_at": "",
            "updated_at": _now(),
        })
        self.store.update_staged(updated)
        self._audit(updated, action="edit", from_status=current.status, to_status="pending",
                    reviewer=reviewer, comment=comment, revision=updated.revision)
        return updated

    # ------------------------------------------------------------------ #
    # 导出前置校验
    # ------------------------------------------------------------------ #
    def assert_approved(self, staging_ids: list[str]) -> list[StagedConversation]:
        """导出前必须确认每一批都是 approved。

        为什么不用 `WHERE status='approved'` 过滤掉未批准的就行：
            那样"漏审"会静默变成"少导" —— 数据少了没人发现。
            显式断言会把问题暴露出来，让流程卡在正确的地方。
        """
        rows: list[StagedConversation] = []
        blocked: list[str] = []
        for sid in staging_ids:
            staged = self.store.get_staged(sid)
            if staged is None or staged.status != "approved":
                blocked.append(sid)
                continue
            rows.append(staged)
        if blocked:
            raise ReviewRequired(f"以下数据尚未通过人工审核，禁止进入下游：{blocked[:10]}")
        return rows

    # ------------------------------------------------------------------ #
    def _require(self, staging_id: str) -> StagedConversation:
        staged = self.store.get_staged(staging_id)
        if staged is None:
            raise InvalidTransition(f"暂存条目不存在：{staging_id}")
        return staged

    def _audit(
        self,
        staged: StagedConversation,
        *,
        action: str,
        from_status: str,
        to_status: str,
        reviewer: str,
        comment: str,
        revision: int,
    ) -> None:
        self.store.append_audit(ReviewRecord(
            audit_id=f"rvw_{uuid4().hex[:10]}",
            staging_id=staged.staging_id,
            action=action,  # type: ignore[arg-type]
            from_status=from_status,
            to_status=to_status,
            reviewer=reviewer or "未署名",
            comment=comment,
            revision=revision,
            created_at=_now(),
        ))
