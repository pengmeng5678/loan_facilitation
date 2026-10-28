"""中文/英文混合分词器（无第三方依赖）。

为什么不用 jieba：
    引入分词器会给部署增加负担（词典、内存、版本），而本场景的检索目标是
    "产品名 / 产品编号 / 专有名词"的精确命中 —— 这类词汇用 **字符 n-gram**
    反而比词粒度更稳（"先息后本""公积金信用贷"这类词不会被切错）。

策略：
    - 英文/数字：按连续字母数字切词（保留产品编号如 XYD-001）
    - 中文：既产出单字，也产出 2-gram / 3-gram
    这样 BM25 既能做精确匹配，也能容忍轻微的字面差异。
"""

from __future__ import annotations

import re

_ASCII_TOKEN = re.compile(r"[a-zA-Z]+|\d+(?:\.\d+)?")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_CJK_RANGE = ("\u4e00", "\u9fff")


def tokenize(text: str) -> list[str]:
    """把文本切成检索用 token 列表。"""
    if not text:
        return []

    lowered = text.lower()
    tokens: list[str] = list(_ASCII_TOKEN.findall(lowered))

    # 中文片段：产出单字 + 2-gram + 3-gram
    for seg in _CJK.findall(text):
        tokens.extend(seg)
        tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        tokens.extend(seg[i : i + 3] for i in range(len(seg) - 2))
        # 单独的字符也作为 token（上面 extend(seg) 已经把每个字加进去了）

    return tokens


def contains_cjk(text: str) -> bool:
    """是否包含中文（用于决定是否需要 n-gram 扩展）。"""
    return any(_CJK_RANGE[0] <= ch <= _CJK_RANGE[1] for ch in text)


def content_tokens(text: str) -> list[str]:
    """只保留"有信息量"的 token：多字词 + 英文数字词，丢掉单个中文字。

    为什么需要它：单字（"的""一""是"）在任何中文文档里都会出现，
    用它算重合度会让**完全无关的问题也拿到不低的相关分**，
    从而导致"该拒答的没拒答"。相关性判定（Reranker / 拒答闸门）必须用它。
    """
    return [t for t in tokenize(text) if len(t) >= 2]
