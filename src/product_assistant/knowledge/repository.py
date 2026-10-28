"""知识库仓储层：从磁盘加载产品目录、政策条款、客户档案。

数据与代码分离的原因：
    政策月月变、产品随时增减，这些是**运营数据**而不是代码。
    所以放在 .data/ 目录下的 JSON/JSONL 中，由装载器读入并建索引。
    生产环境把这里替换成"从银行政策源增量拉取 + 审批入库"即可，接口不变。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from agent_platform.logging_config import get_logger
from product_assistant.domain.models import CustomerProfile, Product
from product_assistant.rag.chunking import PolicyDocument, chunk_document

logger = get_logger(__name__)


class PolicyRepository:
    """产品政策仓储（内存态，启动时加载一次）。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.products: dict[str, Product] = {}
        self.documents: list[PolicyDocument] = []
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        """从 data_dir 加载 products.json 与 policies.jsonl。"""
        self.products.clear()
        self.documents.clear()

        products_file = self.data_dir / "products.json"
        if products_file.exists():
            raw = json.loads(products_file.read_text(encoding="utf-8"))
            for item in raw:
                product = Product(**item)
                self.products[product.code] = product
            logger.info("加载产品目录：%d 个产品", len(self.products))
        else:
            logger.warning("未找到产品目录文件：%s", products_file)

        policies_file = self.data_dir / "policies.jsonl"
        if policies_file.exists():
            count = 0
            for line in policies_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                self.documents.append(
                    PolicyDocument(
                        policy_id=payload["policy_id"],
                        text=payload["text"],
                        product_code=payload.get("product_code", ""),
                        clause_no=payload.get("clause_no", ""),
                        title=payload.get("title", ""),
                        effective_date=_parse_date(payload.get("effective_date")),
                        expire_date=_parse_date(payload.get("expire_date")),
                        source_file=payload.get("source_file", ""),
                        metadata=payload.get("metadata", {}),
                    )
                )
                count += 1
            logger.info("加载政策条款：%d 条", count)
        else:
            logger.warning("未找到政策文件：%s", policies_file)

        self._loaded = True

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def all_chunks(self) -> list:
        """把全部政策条款切成子块（供建索引）。"""
        chunks = []
        for doc in self.documents:
            chunks.extend(chunk_document(doc))
        return chunks

    def get_product(self, code: str) -> Product | None:
        return self.products.get(code)

    def list_products(self) -> list[Product]:
        return list(self.products.values())

    def active_products(self, as_of: date | None = None) -> list[Product]:
        """返回在 as_of 当天有效的产品（时效过滤）。"""
        as_of = as_of or date.today()
        return [p for p in self.products.values() if p.is_active(as_of)]

    def stats(self) -> dict[str, int]:
        return {
            "products": len(self.products),
            "documents": len(self.documents),
            "chunks": len(self.all_chunks()) if self._loaded else 0,
        }


class ProfileStore:
    """客户档案仓储。真实环境来自项目一/四的 CRM 与数据中台。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.profiles: dict[str, CustomerProfile] = {}
        self._loaded = False

    def load(self) -> None:
        self.profiles.clear()
        file = self.data_dir / "customers.json"
        if not file.exists():
            logger.warning("未找到客户档案文件：%s", file)
            self._loaded = True
            return
        raw = json.loads(file.read_text(encoding="utf-8"))
        for item in raw:
            profile = CustomerProfile(**item)
            self.profiles[profile.customer_id] = profile
        logger.info("加载客户档案：%d 位", len(self.profiles))
        self._loaded = True

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def get(self, customer_id: str) -> CustomerProfile | None:
        self.ensure_loaded()
        return self.profiles.get(customer_id)

    def all(self) -> list[CustomerProfile]:
        self.ensure_loaded()
        return list(self.profiles.values())

    def upsert(self, profile: CustomerProfile) -> None:
        """登记/更新客户档案（对应"AI 客服槽位填充后写入档案"）。"""
        self.ensure_loaded()
        self.profiles[profile.customer_id] = profile


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)
