"""火山引擎 RTC 进房 Token 生成（**正式 Token**，不是控制台的临时 Token）。

为什么必须自己算 Token：
    临时 Token 只有 7 天有效期、且是按"人"手工签发的，无法按房间/用户做细粒度授权。
    生产环境必须由服务端用 AppKey 现算 —— 客户端拿到的 Token 只能进指定房间、
    只能以指定 userId 身份出现，泄露了也进不了别的房间。

Token 结构（对齐官方 AccessToken 的 001 方案）：

    001 + {appId} + base64( payload || HMAC-SHA256(appKey, payload) )

    payload = uint32(nonce)          # 随机数，防重放
            + uint32(issuedAt)       # 签发时间（Unix 秒）
            + uint32(expireAt)       # 过期时间（Unix 秒）
            + string(roomId)         # uint32 长度 + UTF-8 字节
            + string(userId)
            + map<uint32,uint32>(privileges)   # 权限位 → 该权限的过期时间
              # 0 = PrivPublishStream（发布音视频流）
              # 4 = PrivSubscribeStream（订阅他人流）

注意几个坑（都是踩过才知道的）：
    * `nonce` 必须是 uint32，用随机浮点直接塞进去会在打包时溢出；
    * 打包用**大端序**（network order），小端签出来的 Token 一定校验失败；
    * `HMAC` 的密钥是 AppKey，签的是 payload 的**原始字节**，不是 base64 后的字符串；
    * 进房时 roomId / userId 必须与签 Token 时**完全一致**，否则报
      ERROR_CODE_INVALID_TOKEN —— 这个错误码看起来像"权限不足"，其实多半是参数不一致。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

from pydantic import BaseModel

TOKEN_VERSION = "001"

# 权限位（对齐官方 AccessToken 的枚举值）
PRIV_PUBLISH_STREAM = 0
PRIV_SUBSCRIBE_STREAM = 4


class TokenVerdict(BaseModel):
    """Token 自校验结果。"""

    valid: bool
    reason: str = ""
    room_id: str = ""
    user_id: str = ""
    issued_at: int = 0
    expire_at: int = 0


def mask_token(token: str, keep: int = 10) -> str:
    """把 Token 打码后再进日志。

    日志/异常堆栈/截图里出现完整 Token，等于把房间权限公开 ——
    Token 有效期通常 24 小时，泄露窗口很长。
    """
    if not token:
        return ""
    if len(token) <= keep * 2:
        return token[:keep] + "***"
    return f"{token[:keep]}...{token[-6:]}（{len(token)} 字符，已脱敏）"


def _u32(value: int) -> bytes:
    """打包 32 位无符号整数（大端）。

    必须显式 `& 0xFFFFFFFF`：Python 的 int 无上界，超范围会在 struct 里抛异常，
    而边界值（如 nonce 恰好等于 0xFFFFFFFF+1）是随机数最容易触发的地方。
    """
    return struct.pack(">I", value & 0xFFFFFFFF)


def _str(value: str) -> bytes:
    """打包字符串：uint32 长度 + UTF-8 字节。"""
    raw = value.encode("utf-8")
    return _u32(len(raw)) + raw


class RtcTokenBuilder:
    """RTC Token 生成器。

    只依赖 AppId / AppKey，不访问网络 —— 所以可以在任何地方（含单元测试）签发 Token。
    """

    def __init__(self, app_id: str, app_key: str) -> None:
        self.app_id = app_id
        self.app_key = app_key

    # ------------------------------------------------------------------ #
    # 打包 / 解包
    # ------------------------------------------------------------------ #
    def _pack_payload(
        self,
        *,
        room_id: str,
        user_id: str,
        nonce: int,
        issued_at: int,
        expire_at: int,
        privileges: dict[int, int],
    ) -> bytes:
        buf = bytearray()
        buf += _u32(nonce)
        buf += _u32(issued_at)
        buf += _u32(expire_at)
        buf += _str(room_id)
        buf += _str(user_id)
        # 权限表：先写个数，再按 key 升序写 (key, value)。
        # 顺序必须固定 —— 否则同样的输入会签出不同的 Token，缓存与自校验都会失效。
        keys = sorted(privileges)
        buf += _u32(len(keys))
        for key in keys:
            buf += _u32(key)
            buf += _u32(privileges[key])
        return bytes(buf)

    @staticmethod
    def _read_u32(data: bytes, offset: int) -> tuple[int, int]:
        return struct.unpack_from(">I", data, offset)[0], offset + 4

    @classmethod
    def _read_str(cls, data: bytes, offset: int) -> tuple[str, int]:
        length, offset = cls._read_u32(data, offset)
        value = data[offset : offset + length].decode("utf-8")
        return value, offset + length

    # ------------------------------------------------------------------ #
    # 签发
    # ------------------------------------------------------------------ #
    def issue(
        self,
        *,
        room_id: str,
        user_id: str,
        ttl_seconds: int = 86_400,
        now: float | None = None,
        nonce: int | None = None,
        privileges: dict[int, int] | None = None,
    ) -> str:
        """签发进房 Token。

        参数：
            room_id / user_id: 必须与客户端 joinRoom 时传的值完全一致
            ttl_seconds:      有效期，官方建议 24 小时内
            now:              注入时间（测试用；生产留空）
            privileges:       自定义权限表；默认"可发布 + 可订阅"
        """
        if not self.app_id or not self.app_key:
            raise ValueError("RTC AppId / AppKey 未配置，无法签发 Token")

        issued_at = int(now if now is not None else time.time())
        expire_at = issued_at + max(1, int(ttl_seconds))
        if nonce is None:
            # secrets 而不是 random：Token 的可预测性直接等于房间权限的可预测性
            nonce = secrets.randbelow(0xFFFFFFFF)

        grants = privileges or {
            PRIV_PUBLISH_STREAM: expire_at,
            PRIV_SUBSCRIBE_STREAM: expire_at,
        }
        payload = self._pack_payload(
            room_id=room_id,
            user_id=user_id,
            nonce=nonce,
            issued_at=issued_at,
            expire_at=expire_at,
            privileges=grants,
        )
        signature = hmac.new(self.app_key.encode("utf-8"), payload, hashlib.sha256).digest()
        body = base64.b64encode(payload + signature).decode("ascii")
        return f"{TOKEN_VERSION}{self.app_id}{body}"

    # ------------------------------------------------------------------ #
    # 自校验
    # ------------------------------------------------------------------ #
    def verify(self, token: str, *, now: float | None = None) -> TokenVerdict:
        """本地校验 Token（签名 + 有效期 + 关键字段）。

        生产环境不需要调用它（RTC 服务端会校验），它的价值在**排障**：
        "为什么进不了房"第一件事就是确认手上这个 Token 是不是自己签的、过没过期、
        房间号有没有写错 —— 有自校验就不用干等云端返回 INVALID_TOKEN。
        """
        if not token:
            return TokenVerdict(valid=False, reason="空 Token")
        head = TOKEN_VERSION + self.app_id
        if not token.startswith(head):
            return TokenVerdict(valid=False, reason=f"前缀不匹配（期望 {TOKEN_VERSION}+AppId）")

        try:
            raw = base64.b64decode(token[len(head) :], validate=True)
        except Exception:  # noqa: BLE001
            return TokenVerdict(valid=False, reason="base64 解码失败")

        if len(raw) <= 32:
            return TokenVerdict(valid=False, reason="长度异常（缺少签名或 payload）")

        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(self.app_key.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, signature):
            return TokenVerdict(valid=False, reason="签名不匹配（AppKey 不对或被篡改）")

        try:
            offset = 0
            nonce, offset = self._read_u32(payload, offset)
            issued_at, offset = self._read_u32(payload, offset)
            expire_at, offset = self._read_u32(payload, offset)
            room_id, offset = self._read_str(payload, offset)
            user_id, offset = self._read_str(payload, offset)
            count, offset = self._read_u32(payload, offset)
            for _ in range(count):
                _key, offset = self._read_u32(payload, offset)
                _value, offset = self._read_u32(payload, offset)
        except Exception:  # noqa: BLE001
            return TokenVerdict(valid=False, reason="payload 解析失败（结构非法）")

        ts = int(now if now is not None else time.time())
        if ts >= expire_at:
            return TokenVerdict(
                valid=False,
                reason=f"已过期（过期于 {expire_at}，当前 {ts}）",
                room_id=room_id,
                user_id=user_id,
                issued_at=issued_at,
                expire_at=expire_at,
            )
        return TokenVerdict(
            valid=True,
            room_id=room_id,
            user_id=user_id,
            issued_at=issued_at,
            expire_at=expire_at,
        )

    def peek(self, token: str) -> TokenVerdict:
        """只解析不校验签名（排障时看字段用）。"""
        head = TOKEN_VERSION + self.app_id
        if not token.startswith(head) or not self.app_id:
            # 换个 AppId 的 Token 也要能看：直接按 3 + appId 长度猜是猜不准的，
            # 所以这里要求调用方传对 builder；猜不准时退化为"无法解析"。
            return TokenVerdict(valid=False, reason="无法解析（AppId 不匹配该 builder）")
        return self.verify(token, now=0)  # now=0 绕过过期判断，只看签名与字段
