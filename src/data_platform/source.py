"""数据源适配器：微信本地 SQLite / JSONL / 离线夹具。

**为什么要有"夹具生成器"**：真实微信库（MSG0.5.db / MicroMsg.db）是个人隐私数据，
不可能进仓库，也不可能在 CI 里跑。所以这里把"抽取逻辑"和"数据"分开：
    · `WeChatSqliteSource` 是真实适配器（列名自适应、只读打开）；
    · `build_wechat_fixture()` 生成一个**结构相同**的库给测试与演示用。
面试被问"你没有真实数据怎么验证"时，这是标准答案：
    适配器按真实库结构写，夹具按同一结构生成，管线端到端跑通；
    生产只需把路径指向真实库。

**只读打开**：`file:{path}?mode=ro` —— 抽取工具绝不能有写坏别人微信库的可能。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from data_platform.domain import ChatSession, RawMessage

#: 微信消息类型 → 占位文本（让分类器能把"媒体消息"识别出来）
_WECHAT_TYPE_TEXT: dict[int, str] = {
    1: "text",
    3: "[图片]",
    34: "[语音]",
    42: "[名片]",
    43: "[视频]",
    47: "[表情]",
    48: "[位置]",
    49: "[文件]",
    50: "[语音通话]",
    10000: "[系统消息]",
    10002: "[系统消息]",
}

#: 允许的列名别名（不同微信版本列名有出入，按先到先得匹配）
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "msg_id": ("msgId", "msg_id", "MsgId", "localId"),
    "session_id": ("talker", "session_id", "strTalker", "Talker"),
    "content": ("content", "Content", "msgContent"),
    "ts": ("createTime", "create_time", "timestamp", "CreateTime"),
    "type": ("type", "msgType", "Type"),
    "is_send": ("isSend", "is_send", "IsSend"),
    "sender": ("sender", "Sender", "fromUser"),
}


class MessageSource(Protocol):
    """消息源协议（任何实现了它的人都能接进管线）。"""

    name: str

    def iter_messages(self, *, limit: int = 0) -> Iterator[RawMessage]: ...


# --------------------------------------------------------------------------- #
# 会话聚合
# --------------------------------------------------------------------------- #
def load_sessions(messages: Iterable[RawMessage]) -> list[ChatSession]:
    """把消息按 session_id 聚合成会话（保持时间顺序）。"""
    by_session: dict[str, ChatSession] = {}
    for msg in messages:
        session = by_session.get(msg.session_id)
        if session is None:
            session = ChatSession(
                session_id=msg.session_id,
                customer_id=_derive_customer_id(msg.session_id),
                customer_name=msg.sender_name if msg.role == "customer" else "",
                is_group=msg.is_group,
            )
            by_session[msg.session_id] = session
        session.messages.append(msg)
        if not session.customer_name and msg.role == "customer" and msg.sender_name:
            session.customer_name = msg.sender_name
    for session in by_session.values():
        session.messages.sort(key=lambda m: (m.ts, m.msg_id))
    return list(by_session.values())


def _derive_customer_id(session_id: str) -> str:
    """从会话标识推客户编号（群聊取群号，单聊取 wxid 尾部）。"""
    raw = session_id.replace("@chatroom", "")
    return f"CU_{raw[-8:]}" if raw else "CU_unknown"


# --------------------------------------------------------------------------- #
# JSONL 源（演示数据 / 项目三语料格式）
# --------------------------------------------------------------------------- #
class JsonlSource:
    """读取 `{"conversation_id", "started_at", "messages":[...]}` 形式的 JSONL。

    这是仓库里已有的语料格式（项目三/七用的同一份），
    所以它同时充当"把老语料接进新中台"的迁移入口。
    """

    name = "jsonl"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def iter_messages(self, *, limit: int = 0) -> Iterator[RawMessage]:
        if not self.path.exists():
            return
        count = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            conversation_id = str(row.get("conversation_id") or row.get("session_id") or "")
            agent = str(row.get("agent") or "业务员")
            base = _parse_ts(row.get("started_at"))
            for i, msg in enumerate(row.get("messages") or []):
                role = "business" if str(msg.get("role")) in ("business", "agent", "assistant") else "customer"
                ts = base + float(msg.get("ts") or 0)
                yield RawMessage(
                    msg_id=f"{conversation_id}-{i}",
                    session_id=conversation_id,
                    sender_wxid="self" if role == "business" else str(row.get("customer_id") or ""),
                    sender_name=agent if role == "business" else str(row.get("customer_id") or ""),
                    role=role,  # type: ignore[arg-type]
                    content=str(msg.get("text") or msg.get("content") or ""),
                    ts=ts,
                    msg_type="text",
                )
                count += 1
                if limit and count >= limit:
                    return


def _parse_ts(value: Any) -> float:
    """把 `2026-08-01T10:00:00` / 秒 / 毫秒 统一成秒。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return _normalize_epoch(float(value))
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return _normalize_epoch(float(text))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _normalize_epoch(value: float) -> float:
    """毫秒时间戳（> 1e11）按毫秒处理 —— 微信不同版本单位不一致。"""
    return value / 1000.0 if value > 1e11 else value


# --------------------------------------------------------------------------- #
# 微信 SQLite 源
# --------------------------------------------------------------------------- #
class WeChatSqliteSource:
    """真实微信本地库适配器（只读）。

    抽取要解决的四件事（也就是"为什么不能直接 select 出来用"）：
      ① **wxid → 显示名**：库里存 `wxid_abc123`，业务员认的是"张先生"；
      ② **群消息发送者**：群聊的 talker 是群号，真实发送者在内容前缀 `wxid_xxx:\\n` 里；
      ③ **消息类型**：图片/语音没有文本，得转成占位符，否则整条被当空内容丢掉；
      ④ **时间单位**：createTime 有秒也有毫秒版本。
    """

    name = "wechat_sqlite"

    def __init__(
        self,
        path: str | Path,
        *,
        message_table: str = "message",
        contact_table: str = "rcontact",
        self_wxid: str = "self",
    ) -> None:
        self.path = Path(path)
        self.message_table = message_table
        self.contact_table = contact_table
        self.self_wxid = self_wxid

    # ------------------------------------------------------------------ #
    def iter_messages(self, *, limit: int = 0) -> Iterator[RawMessage]:
        if not self.path.exists():
            raise FileNotFoundError(f"微信源库不存在：{self.path}（先运行 scripts/seed_data.py）")
        # mode=ro：抽取工具绝不能写别人的库
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            columns = self._detect_columns(conn)
            names = self._load_contacts(conn, columns)
            sql = (
                f"SELECT {columns['msg_id']} AS msg_id, {columns['session_id']} AS session_id, "
                f"{columns['content']} AS content, {columns['ts']} AS ts, "
                f"{columns['type']} AS type, {columns['is_send']} AS is_send"
                + (f", {columns['sender']} AS sender" if columns.get("sender") else "")
                + f" FROM {self.message_table} ORDER BY {columns['ts']} ASC"
            )
            count = 0
            for row in conn.execute(sql):
                msg = self._to_message(row, names, has_sender=bool(columns.get("sender")))
                if msg is None:
                    continue
                yield msg
                count += 1
                if limit and count >= limit:
                    return
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    def _detect_columns(self, conn: sqlite3.Connection) -> dict[str, str]:
        """按别名表探测列名；缺关键列时给出可操作的报错。"""
        try:
            info = conn.execute(f"PRAGMA table_info({self.message_table})").fetchall()
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                f"无法读取表 {self.message_table}：{exc}。"
                "请确认路径指向微信 MSG 库（含 message 表）"
            ) from exc
        existing = {str(row[1]) for row in info}
        if not existing:
            raise ValueError(f"表 {self.message_table} 不存在或没有列，路径可能不是微信消息库")

        resolved: dict[str, str] = {}
        for key, aliases in _COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in existing:
                    resolved[key] = alias
                    break
        missing = [k for k in ("msg_id", "session_id", "content", "ts") if k not in resolved]
        if missing:
            raise ValueError(
                f"微信消息表缺少必要列：{missing}；已识别列：{sorted(existing)}。"
                "请用 WECHAT_MESSAGE_TABLE / 列映射适配你的微信版本"
            )
        resolved.setdefault("type", resolved["content"])  # 没有 type 列就当纯文本
        resolved.setdefault("is_send", resolved["msg_id"])  # 没有 isSend 就当全部接收
        return resolved

    def _load_contacts(self, conn: sqlite3.Connection, columns: dict[str, str]) -> dict[str, str]:
        """wxid → 显示名。优先备注，其次昵称。"""
        try:
            info = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({self.contact_table})")}
        except sqlite3.DatabaseError:
            return {}
        if not info:
            return {}
        user_col = next((c for c in ("username", "UserName", "userName") if c in info), "")
        if not user_col:
            return {}
        name_cols = [c for c in ("remark", "conRemark", "remarkName", "nickname", "NickName") if c in info]
        if not name_cols:
            return {}
        select = ", ".join(f"{c}" for c in [user_col, *name_cols])
        names: dict[str, str] = {}
        for row in conn.execute(f"SELECT {select} FROM {self.contact_table}"):
            wxid = str(row[user_col] or "").strip()
            if not wxid:
                continue
            display = ""
            for col in name_cols:
                value = str(row[col] or "").strip()
                if value:
                    display = value
                    break
            if display:
                names[wxid] = display
        return names

    def _to_message(
        self, row: sqlite3.Row, names: dict[str, str], *, has_sender: bool
    ) -> RawMessage | None:
        session_id = str(row["session_id"] or "").strip()
        if not session_id:
            return None
        raw_content = str(row["content"] or "")
        is_group = session_id.endswith("@chatroom")

        sender_wxid = str(row["sender"] or "").strip() if has_sender else session_id
        content = raw_content

        # 群消息：内容前缀 "wxid_xxx:\n真实内容"
        if is_group and ":\n" in raw_content:
            prefix, rest = raw_content.split(":\n", 1)
            if prefix and len(prefix) <= 64 and " " not in prefix:
                sender_wxid = prefix
                content = rest

        try:
            type_code = int(row["type"] or 1)
        except (TypeError, ValueError):
            type_code = 1
        placeholder = _WECHAT_TYPE_TEXT.get(type_code)
        if placeholder and placeholder != "text":
            # 非文本消息：保留占位符（分类器会按"媒体消息"处理），不丢事件
            content = content.strip() or placeholder
            msg_type = placeholder.strip("[]")
        else:
            msg_type = "text"
            content = content.strip()

        try:
            is_send = int(row["is_send"] or 0) == 1
        except (TypeError, ValueError):
            is_send = False
        if self.self_wxid and sender_wxid == self.self_wxid:
            is_send = True

        role = "business" if is_send else "customer"
        sender_name = names.get(sender_wxid, "") if sender_wxid else ""
        if not sender_name:
            sender_name = "业务员" if is_send else "客户"

        return RawMessage(
            msg_id=str(row["msg_id"]),
            session_id=session_id,
            sender_wxid=sender_wxid,
            sender_name=sender_name,
            role=role,  # type: ignore[arg-type]
            content=content,
            ts=_normalize_epoch(float(row["ts"] or 0)),
            msg_type=msg_type,
            is_group=is_group,
        )


# --------------------------------------------------------------------------- #
# 离线夹具：生成一个"结构相同"的微信库
# --------------------------------------------------------------------------- #
def build_wechat_fixture(
    path: str | Path,
    sessions: Sequence[dict[str, Any]],
    *,
    base_ts: float = 1_790_000_000,
) -> Path:
    """按微信库结构生成夹具（message + rcontact），供测试与演示使用。

    `sessions` 每一项：
        {"session_id": "...", "customer_name": "...", "is_group": false,
         "messages": [{"role": "business"|"customer", "content": "...", "offset": 0}, ...]}
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                msgId INTEGER PRIMARY KEY AUTOINCREMENT,
                type INTEGER NOT NULL DEFAULT 1,
                isSend INTEGER NOT NULL DEFAULT 0,
                createTime INTEGER NOT NULL,
                talker TEXT NOT NULL,
                content TEXT,
                sender TEXT
            );
            CREATE TABLE rcontact (
                username TEXT PRIMARY KEY,
                nickname TEXT,
                remark TEXT
            );
            """
        )
        msg_id = 0
        for session in sessions:
            session_id = str(session.get("session_id") or "")
            customer_name = str(session.get("customer_name") or "")
            if session_id and customer_name:
                conn.execute(
                    "INSERT OR REPLACE INTO rcontact (username, nickname, remark) VALUES (?, ?, ?)",
                    (session_id, customer_name, customer_name),
                )
            for msg in session.get("messages") or []:
                msg_id += 1
                role = str(msg.get("role") or "customer")
                is_send = 1 if role == "business" else 0
                msg_type = int(msg.get("type") or (1 if str(msg.get("content", "")).startswith("[") else 1))
                conn.execute(
                    "INSERT INTO message (type, isSend, createTime, talker, content, sender) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        msg_type,
                        is_send,
                        int(base_ts + float(msg.get("offset") or 0)),
                        session_id,
                        str(msg.get("content") or ""),
                        "self" if is_send else session_id,
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return path


#: 默认演示数据：覆盖 9 类边界（系统/媒体/敏感/广告/过短/闲聊/重复/普通/高价值）
DEMO_SESSIONS: list[dict[str, Any]] = [
    {
        "session_id": "wxid_demo_zhang",
        "customer_name": "张先生",
        "messages": [
            {"role": "customer", "content": "你好，我想问下公积金能不能贷？", "offset": 0},
            {"role": "business", "content": "您好，我是助贷顾问小李。您公积金缴存多久了？近半年征信查询几次？", "offset": 40},
            {"role": "customer", "content": "交了 3 年，查询大概 3 次，我有套房还在按揭，名下还有 3 笔网贷共 15 万，想置换成银行低息贷款。", "offset": 90},
            {"role": "business", "content": "明白。您这种情况可以做债务置换：先把高息网贷结清，再走公积金信用贷，年化大概 3.2% 到 4.6%，额度 20 万到 50 万，具体以银行审批为准。", "offset": 150},
            {"role": "customer", "content": "利率能再低点吗？我朋友说能做到 3% 以下。", "offset": 210},
            {"role": "business", "content": "3% 以下目前做不到，任何承诺具体利率的说法都不合规。您的优势是公积金连续缴存，我们可以按最优档申请，结果以审批为准。", "offset": 260},
            {"role": "customer", "content": "行，那我周六上午过来一趟，我电话 13812345678，身份证 320102199001011234。", "offset": 300},
            {"role": "business", "content": "好的，周六上午 10 点给您留位，到店带身份证和公积金缴存明细就行。", "offset": 340},
            {"role": "customer", "content": "好，签了合同是不是就能放款？", "offset": 380},
            {"role": "business", "content": "签约后还要银行审批，一般 1 到 3 个工作日出结果，放款以银行实际进度为准。", "offset": 420},
        ],
    },
    {
        "session_id": "wxid_demo_li",
        "customer_name": "李女士",
        "messages": [
            {"role": "customer", "content": "你们这是骗人的吧？说好的包过，现在又说办不了，我要投诉你们！", "offset": 0},
            {"role": "business", "content": "非常抱歉给您带来困扰，我先核实一下您的申请记录，马上回复您。", "offset": 60},
            {"role": "customer", "content": "再不处理我就打 12378 举报。", "offset": 120},
        ],
    },
    {
        "session_id": "wxid_demo_spam",
        "customer_name": "推广号",
        "messages": [
            {"role": "customer", "content": "秒批！无需审核！日赚 500，加我微信领取通道 http://spam.example.com", "offset": 0},
            {"role": "customer", "content": "代理加盟，点击链接了解详情，免费办理", "offset": 20},
        ],
    },
    {
        "session_id": "wxid_demo_media",
        "customer_name": "王先生",
        "messages": [
            {"role": "customer", "content": "[图片]", "offset": 0, "type": 3},
            {"role": "business", "content": "[语音]", "offset": 15, "type": 34},
        ],
    },
    {
        "session_id": "wxid_demo_short",
        "customer_name": "赵先生",
        "messages": [
            {"role": "customer", "content": "在吗", "offset": 0},
            {"role": "business", "content": "在的", "offset": 10},
        ],
    },
    {
        "session_id": "wxid_demo_group@chatroom",
        "customer_name": "助贷交流群",
        "messages": [
            {"role": "customer", "content": "wxid_member_a:\n请问征信查询 8 次还能做公积金贷吗？", "offset": 0},
            {"role": "business", "content": "可以看具体查询结构，近 3 个月硬查询超过 8 次要评估，先别继续申请了。", "offset": 30},
            {"role": "customer", "content": "wxid_member_a:\n明白了，那需要准备哪些资料？", "offset": 60},
            {"role": "business", "content": "身份证、公积金缴存明细、征信报告，三样齐了就能先做预审。", "offset": 90},
        ],
    },
    {
        # 与第一条会话内容重复（用于验证跨块去重）
        "session_id": "wxid_demo_zhang_copy",
        "customer_name": "张先生",
        "messages": [
            {"role": "customer", "content": "你好，我想问下公积金能不能贷？", "offset": 0},
            {"role": "business", "content": "您好，我是助贷顾问小李。您公积金缴存多久了？近半年征信查询几次？", "offset": 40},
            {"role": "customer", "content": "交了 3 年，查询大概 3 次，我有套房还在按揭，名下还有 3 笔网贷共 15 万，想置换成银行低息贷款。", "offset": 90},
            {"role": "business", "content": "明白。您这种情况可以做债务置换：先把高息网贷结清，再走公积金信用贷，年化大概 3.2% 到 4.6%，额度 20 万到 50 万，具体以银行审批为准。", "offset": 150},
            {"role": "customer", "content": "利率能再低点吗？我朋友说能做到 3% 以下。", "offset": 210},
            {"role": "business", "content": "3% 以下目前做不到，任何承诺具体利率的说法都不合规。您的优势是公积金连续缴存，我们可以按最优档申请，结果以审批为准。", "offset": 260},
            {"role": "customer", "content": "行，那我周六上午过来一趟，我电话 13812345678，身份证 320102199001011234。", "offset": 300},
            {"role": "business", "content": "好的，周六上午 10 点给您留位，到店带身份证和公积金缴存明细就行。", "offset": 340},
            {"role": "customer", "content": "好，签了合同是不是就能放款？", "offset": 380},
            {"role": "business", "content": "签约后还要银行审批，一般 1 到 3 个工作日出结果，放款以银行实际进度为准。", "offset": 420},
        ],
    },
]
