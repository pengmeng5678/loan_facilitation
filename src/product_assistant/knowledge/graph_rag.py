"""GraphRAG 产品关系图谱（项目二的"多跳推理"能力）。

纯向量检索解决不了的问题：
    客户条件"征信查询多 + 有按揭房 + 想做置换" —— 需要跨实体推理：
        查询多 → 哪些产品能容忍
        有按揭房 → 可做抵押类
        想置换 → 哪些产品支持"债务置换"用途
        某产品被拒 → 有没有"用途相近、准入更宽"的**替代产品**
    这正是图谱的用武之地。

实现方式：
    用轻量邻接表建图（节点 = 产品 / 资质 / 用途；边 = 具备/支持/可替代），
    支持 1~2 跳查询与衰减评分。生产环境换成 Neo4j 只需替换本类实现，
    上层 service 的调用方式不变（这是刻意的替换点）。
"""

from __future__ import annotations

from collections import defaultdict

from agent_platform.logging_config import get_logger
from product_assistant.domain.models import Product

logger = get_logger(__name__)


class ProductGraph:
    """产品-资质-用途 关系图谱。"""

    def __init__(self) -> None:
        # 邻接表：node -> {neighbor: weight}
        self._adj: dict[str, dict[str, float]] = defaultdict(dict)
        self._product_nodes: dict[str, Product] = {}

    # ------------------------------------------------------------------ #
    # 建图
    # ------------------------------------------------------------------ #
    def build(self, products: list[Product]) -> None:
        """按产品目录建图。

        节点命名约定（前缀区分类型，便于调试时一眼看懂）：
            p:<产品编号>      产品
            q:<资质>          资质
            u:<用途>          用途
        """
        self._adj.clear()
        self._product_nodes.clear()

        for product in products:
            pnode = f"p:{product.code}"
            self._product_nodes[product.code] = product

            # 产品 —— 资质（具备）双向边，权重 = 1/资质数量（越专一权重越高）
            q_weight = 1.0 / max(1, len(product.qualifications))
            for q in product.qualifications:
                qnode = f"q:{q}"
                self._adj[pnode][qnode] = q_weight
                self._adj[qnode][pnode] = 1.0

            # 产品 —— 用途（支持）双向边
            u_weight = 1.0 / max(1, len(product.purposes))
            for u in product.purposes:
                unode = f"u:{u}"
                self._adj[pnode][unode] = u_weight
                self._adj[unode][pnode] = 1.0

            # 产品 —— 替代产品（权重 0.6，表示"用途相近但不是同一个"）
            for sub in product.substitutes:
                snode = f"p:{sub}"
                self._adj[pnode][snode] = 0.6

        logger.info("产品图谱构建完成：%d 个节点", len(self._adj))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def substitutes(self, code: str, hops: int = 1) -> list[tuple[str, float]]:
        """查询可替代产品（多跳会衰减）。

        业务价值：主力产品被拒时，业务员需要立刻知道"还能推哪一个"。
        """
        return self._walk(f"p:{code}", hops=hops, kinds=("p",), exclude=[f"p:{code}"])

    def recommend_by_tags(
        self,
        qualifications: list[str],
        purposes: list[str] | None = None,
        *,
        hops: int = 2,
        exclude: set[str] | None = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """按"客户资质 + 用途"做多跳推荐。

        路径示例（2 跳）：
            q:公积金 → p:BANK-A-GJJ-001 → u:债务置换
        即"有公积金 → 这款公积金贷支持债务置换"，正是复杂组合推荐的推理链。
        """
        purposes = purposes or []
        exclude = exclude or set()

        # 起点：客户资质 + 需求用途
        starts: list[str] = [f"q:{q}" for q in qualifications] + [f"u:{u}" for u in purposes]
        scores: dict[str, float] = defaultdict(float)

        for start in starts:
            if start not in self._adj:
                continue
            for neighbor, weight in self._adj[start].items():
                if not neighbor.startswith("p:"):
                    continue
                code = neighbor[2:]
                if code in exclude:
                    continue
                scores[code] += weight
                if hops >= 2:
                    # 第二跳：经产品再走到"替代产品"
                    for n2, w2 in self._adj[neighbor].items():
                        if n2.startswith("p:"):
                            c2 = n2[2:]
                            if c2 in exclude or c2 == code:
                                continue
                            scores[c2] += weight * w2 * 0.5  # 衰减

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        return ranked[:top_k]

    def neighbors(self, code: str) -> dict[str, list[str]]:
        """返回某产品的直接邻居（调试与可解释性用）。"""
        node = f"p:{code}"
        out: dict[str, list[str]] = {"qualifications": [], "purposes": [], "substitutes": []}
        for neighbor in self._adj.get(node, {}):
            kind, _, name = neighbor.partition(":")
            if kind == "q":
                out["qualifications"].append(name)
            elif kind == "u":
                out["purposes"].append(name)
            elif kind == "p":
                out["substitutes"].append(name)
        return out

    def _walk(
        self, start: str, *, hops: int, kinds: tuple[str, ...], exclude: list[str]
    ) -> list[tuple[str, float]]:
        """从 start 出发做 BFS，累计权重（每跳衰减 0.5^hop）。"""
        frontier: dict[str, float] = {start: 1.0}
        seen: set[str] = {start}
        scores: dict[str, float] = defaultdict(float)

        for hop in range(1, hops + 1):
            next_frontier: dict[str, float] = {}
            for node, acc in frontier.items():
                for neighbor, weight in self._adj.get(node, {}).items():
                    if neighbor in seen:
                        continue
                    gain = acc * weight * (0.5 ** (hop - 1))
                    if neighbor.partition(":")[0] in kinds and neighbor not in exclude:
                        scores[neighbor] += gain
                    next_frontier[neighbor] = next_frontier.get(neighbor, 0.0) + gain
                    seen.add(neighbor)
            frontier = next_frontier
            if not frontier:
                break

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        return [(_name_only(node), round(score, 4)) for node, score in ranked]

    def product(self, code: str) -> Product | None:
        return self._product_nodes.get(code)

    def stats(self) -> dict[str, int]:
        return {
            "nodes": len(self._adj),
            "products": len(self._product_nodes),
            "edges": sum(len(v) for v in self._adj.values()) // 2,  # 双向边去重
        }


def _name_only(node: str) -> str:
    return node.partition(":")[2]
