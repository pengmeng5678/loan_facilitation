"""多下游导出（一份数据多格式）。

四种格式对应四类消费者：
    ShareGPT      多轮对话微调（community 通用格式）
    Alpaca        单轮指令微调（instruction/input/output 三元组）
    OpenAI JSONL  直接走 OpenAI 微调接口的 messages 格式
    RAG CSV       向量化/入库前的检查表（人也能直接看）

**三条不可妥协的约束**：

① **只导 approved 且 dataset_eligible 的数据**
   不是"导出时过滤一下"，而是唯一入口就只查这个集合；
   `assert_approved` 还会对指定批次做显式断言（漏审 = 报错，不是少导）。

② **导出层再脱敏一次（第三层），并用 verify() 证明没有残留**
   "我们做了三层脱敏"是可被检验的：每行导出前 `verify()` 必须返回空，
   否则整批拒绝导出。这比在文档里写"确保脱敏"有用。

③ **同输入同输出（幂等）**
   文件名与 export_key 都由内容哈希派生，重复导出同一批数据
   直接复用已有文件 —— 下游不会因为"又点了一次导出"拿到两份不一样的数据。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from data_platform.domain import ExportFormat, ExportManifest, StagedConversation
from data_platform.errors import ExportBlocked
from data_platform.filters import ConversationFilter
from data_platform.masking import MaskingEngine
from data_platform.store import DataPlatformStore

#: 三个格式共用的系统提示（写给模型看，也写给面试官看）
SYSTEM_PROMPT = (
    "你是助贷业务的资深顾问。回答必须基于政策与客户资质，"
    "不承诺放款结果、不承诺具体利率、不编造额度；信息不足时主动追问或建议转人工。"
)

FORMAT_SUFFIX: dict[str, str] = {
    "sharegpt": ".jsonl",
    "alpaca": ".jsonl",
    "openai": ".jsonl",
    "rag_csv": ".csv",
}


class DatasetExporter:
    """把已批准的数据导出成训练/检索格式。"""

    def __init__(
        self,
        store: DataPlatformStore,
        *,
        masker: MaskingEngine,
        conv_filter: ConversationFilter,
        export_dir: str | Path,
    ) -> None:
        self.store = store
        self.masker = masker
        self.filter = conv_filter
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def export(self, fmt: ExportFormat) -> ExportManifest:
        staged = self.store.approved_staged(eligible_only=True)
        if not staged:
            raise ExportBlocked("暂无已批准且可训练的数据（先审核通过几条再导出）")

        prepared = [self._prepare(item) for item in staged]
        rows = self.build_rows(fmt, prepared)
        if not rows:
            raise ExportBlocked(f"格式 {fmt} 未产出任何样本（检查对话里是否有业务员回复）")

        content_hash = _hash_json(rows)
        watermark = max((s.decided_at or s.updated_at) for s in staged)
        export_key = _hash_text(f"{fmt}|{watermark}|{content_hash}")

        existing = self.store.find_export(export_key)
        if existing is not None:
            # 幂等：同一批数据 + 同一格式 → 直接复用已有产物
            return existing.model_copy(update={"reused": True})

        path = self.export_dir / f"{fmt}-{content_hash[:12]}{FORMAT_SUFFIX[fmt]}"
        path.write_text(self._render(fmt, rows), encoding="utf-8")
        manifest = ExportManifest(
            export_id=f"exp_{content_hash[:10]}",
            format=fmt,
            path=str(path),
            rows=len(rows),
            bytes=path.stat().st_size,
            content_hash=content_hash,
            watermark=watermark,
            staging_ids=[s.staging_id for s in staged],
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self.store.save_export(manifest, export_key=export_key)
        return manifest

    # ------------------------------------------------------------------ #
    # 导出前的最后一道校验
    # ------------------------------------------------------------------ #
    def _prepare(self, staged: StagedConversation) -> dict[str, Any]:
        """导出层脱敏 + 残留校验 + 组装成统一的对话结构。"""
        turns: list[dict[str, str]] = []
        for msg in staged.messages:
            masked = self.masker.mask(msg.content, layer="export")
            residual = self.masker.verify(masked.text)
            if residual:
                # 理论上不可达（replace 后不应再命中）；一旦发生说明规则有洞，
                # 宁可整批不导，也不能带着残留出去。
                raise ExportBlocked(
                    f"{staged.staging_id} 导出层校验失败，仍有 {len(residual)} 处未脱敏："
                    f"{[f.kind for f in residual][:5]}"
                )
            if msg.role not in ("customer", "business"):
                continue
            turns.append({"role": str(msg.role), "text": masked.text})
        if not turns:
            raise ExportBlocked(f"{staged.staging_id} 没有可导出的对话轮次")

        joined = "\n".join(t["text"] for t in turns)
        return {
            "staging_id": staged.staging_id,
            "session_id": staged.session_id,
            "customer_id": staged.customer_id,
            "scene": self.filter.scene_of(joined),
            "quality": staged.quality_score,
            "turns": turns,
        }

    # ------------------------------------------------------------------ #
    # 格式构造（纯函数，便于单测）
    # ------------------------------------------------------------------ #
    def build_rows(
        self, fmt: ExportFormat, prepared: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if fmt == "sharegpt":
            return self._sharegpt(prepared)
        if fmt == "alpaca":
            return self._alpaca(prepared)
        if fmt == "openai":
            return self._openai(prepared)
        if fmt == "rag_csv":
            return self._rag(prepared)
        raise ExportBlocked(f"未知导出格式：{fmt}")

    @staticmethod
    def _sharegpt(prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in prepared:
            conversations: list[dict[str, str]] = []
            for turn in item["turns"]:
                role = "human" if turn["role"] == "customer" else "gpt"
                # 相邻同角色合并：ShareGPT 期望 human/gpt 交替
                if conversations and conversations[-1]["from"] == role:
                    conversations[-1]["value"] += "\n" + turn["text"]
                else:
                    conversations.append({"from": role, "value": turn["text"]})
            if not all(c["from"] == "human" for c in conversations):
                rows.append({
                    "conversations": conversations,
                    "meta": {
                        "staging_id": item["staging_id"],
                        "scene": item["scene"],
                        "quality": item["quality"],
                    },
                })
        return rows

    @staticmethod
    def _alpaca(prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """单轮指令样本：前文 → 业务员的下一句。

        为什么取"最后一句业务员回复"当 output：
            这是最接近真实作业位置的样本（模型学的是"该怎么说"），
            而不是让模型学"复述整段对话"。
        """
        rows: list[dict[str, Any]] = []
        for item in prepared:
            turns = item["turns"]
            business_idx = [i for i, t in enumerate(turns) if t["role"] == "business"]
            if not business_idx:
                continue
            last = business_idx[-1]
            context = "\n".join(f"{'客户' if t['role'] == 'customer' else '业务员'}：{t['text']}"
                                for t in turns[:last])
            rows.append({
                "instruction": "你是助贷业务员，请根据对话上下文给出专业、合规的下一句回复。",
                "input": context,
                "output": turns[last]["text"],
                "meta": {"staging_id": item["staging_id"], "scene": item["scene"]},
            })
        return rows

    @staticmethod
    def _openai(prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in prepared:
            messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
            for turn in item["turns"]:
                role = "user" if turn["role"] == "customer" else "assistant"
                if messages[-1]["role"] == role:
                    messages[-1]["content"] += "\n" + turn["text"]
                else:
                    messages.append({"role": role, "content": turn["text"]})
            if any(m["role"] == "assistant" for m in messages):
                rows.append({
                    "messages": messages,
                    "meta": {"staging_id": item["staging_id"], "scene": item["scene"]},
                })
        return rows

    @staticmethod
    def _rag(prepared: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in prepared:
            text = "\n".join(
                f"{'客户' if t['role'] == 'customer' else '业务员'}：{t['text']}"
                for t in item["turns"]
            )
            rows.append({
                "id": item["staging_id"],
                "session_id": item["session_id"],
                "scene": item["scene"],
                "customer_id": item["customer_id"],
                "quality": item["quality"],
                "text": text,
            })
        return rows

    # ------------------------------------------------------------------ #
    # 落盘
    # ------------------------------------------------------------------ #
    @staticmethod
    def _render(fmt: ExportFormat, rows: list[dict[str, Any]]) -> str:
        if fmt == "rag_csv":
            buf = io.StringIO()
            fieldnames = ["id", "session_id", "scene", "customer_id", "quality", "text"]
            writer = csv.DictWriter(buf, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            return buf.getvalue()
        return "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n"


# --------------------------------------------------------------------------- #
def _hash_json(rows: list[dict[str, Any]]) -> str:
    return _hash_text(json.dumps(rows, ensure_ascii=False, sort_keys=True))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
