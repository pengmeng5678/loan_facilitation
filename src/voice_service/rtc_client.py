"""火山引擎 RTC OpenAPI 客户端（把云端 Agent 拉进房间 / 挂断 / 更新配置）。

为什么要包一层而不是业务里直接 `httpx.post`：
    1. **离线可跑**：传输层是可替换的（`RtcTransport`），默认在没配密钥时用
       `RecordingTransport` 把请求原样记下来 —— 单元测试能断言"下发的配置对不对"，
       而不用真的去开房间（开房要钱，而且 CI 里没法开会话）；
    2. **错误可分类**：RTC 的报错五花八门（签名错、AppId 错、房间不存在、
       同一房间已有 Agent），业务层只关心"能不能继续"，所以这里统一映射成
       `ok / error`，并在 error 里保留原始 Code，方便排障；
    3. **敏感信息不外泄**：请求体里有回调 ApiKey，返回给调用方/日志时必须打码。

一句话：**这一层是"别人家的服务"，边界必须明确** —— 它挂了我们只降级，不影响其他业务。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from voice_service.domain import RtcCallResult
from voice_service.volc_sign import VolcCredentials, sign_request

logger = get_logger(__name__)

RTC_HOST = "rtc.volcengineapi.com"
RTC_API_VERSION = "2024-12-01"

# 敏感字段（返回/记日志时打码）
_SENSITIVE_KEYS = {"ApiKey", "apikey", "Token", "token", "Authorization"}


class RtcTransport(Protocol):
    """传输层抽象：只要能把 (url, headers, body) 发出去并拿回 (status, json)。"""

    def post(
        self, *, url: str, params: dict[str, str], headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, dict[str, Any]]: ...


class HttpTransport:
    """真实 HTTP 传输（火山 OpenAPI）。"""

    def post(
        self, *, url: str, params: dict[str, str], headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, dict[str, Any]]:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, params=params, headers=headers, content=body)
            try:
                return resp.status_code, resp.json()
            except json.JSONDecodeError:
                return resp.status_code, {"_raw": resp.text[:500]}


class RecordingTransport:
    """离线传输：不发请求，只记录，然后返回一个"成功"的假响应。

    这是本项目"零密钥可跑通"的关键之一：
    * 测试可以断言我们**下发了什么配置**（ASR 是不是双向流、TTS 音色对不对、
      回调地址是不是指向自己）；
    * `scripts/demo.py` 可以完整演示"开房 → AI 进房 → 挂断"的编排，
      不需要真的花钱开会话。

    代价是：它**不能证明真实链路通**。所以 `RtcCallResult.dry_run=True`
    会一路透到 HTTP 响应里，前端/运维一眼能看出"这是演练"。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(
        self, *, url: str, params: dict[str, str], headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, dict[str, Any]]:
        payload = json.loads(body.decode("utf-8") or "{}")
        self.calls.append({"url": url, "params": dict(params), "payload": payload})
        action = params.get("Action", "Unknown")
        return 200, {
            "ResponseMetadata": {
                "RequestId": f"dryrun-{len(self.calls):04d}-{action}",
                "Action": action,
                "Version": RTC_API_VERSION,
                "Service": "rtc",
                "Region": "dry-run",
            },
            "Result": {"ok": True, "dry_run": True},
        }


def _mask(value: Any) -> Any:
    """递归给敏感字段打码（返回给调用方/写日志之前必过一遍）。"""
    if isinstance(value, dict):
        return {k: ("***" if k in _SENSITIVE_KEYS and v else _mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


class RtcClient:
    """RTC OpenAPI 客户端。"""

    def __init__(self, settings: Settings, *, transport: RtcTransport | None = None) -> None:
        self.settings = settings
        self.credentials = VolcCredentials(settings.volc_access_key, settings.volc_secret_key)
        self.dry_run = not (settings.voice_enabled and self.credentials.configured)
        self.transport: RtcTransport = transport or (HttpTransport() if not self.dry_run else RecordingTransport())
        if self.dry_run:
            logger.warning(
                "RTC 处于演练模式（VOICE_ENABLED=false 或未配 AK/SK）：请求只记录不下发。"
                " 真实开房请配置 VOICE_ENABLED=true + VOLC_ACCESS_KEY/VOLC_SECRET_KEY"
            )

    # ------------------------------------------------------------------ #
    # 内部：一次带签名的调用
    # ------------------------------------------------------------------ #
    def _call(self, action: str, payload: dict[str, Any], *, timeout: float = 10.0) -> RtcCallResult:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        query = {"Action": action, "Version": RTC_API_VERSION}
        url = f"https://{RTC_HOST}/"

        headers = {"Content-Type": "application/json", "Host": RTC_HOST}
        if not self.dry_run:
            headers.update(
                sign_request(
                    credentials=self.credentials,
                    method="POST",
                    host=RTC_HOST,
                    path="/",
                    query=query,
                    body=body,
                    region=self.settings.rtc_region,
                    service=self.settings.rtc_service,
                )
            )

        try:
            status, data = self.transport.post(
                url=url, params=query, headers=headers, body=body, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常不应炸掉调用方
            logger.exception("RTC OpenAPI 调用失败：%s", action)
            return RtcCallResult(
                action=action,
                ok=False,
                dry_run=self.dry_run,
                error=f"{type(exc).__name__}: {exc}",
                payload=_mask(payload),
            )

        meta = (data or {}).get("ResponseMetadata", {})
        error = meta.get("Error") or {}
        ok = status == 200 and not error
        result: RtcCallResult = RtcCallResult(
            action=action,
            ok=ok,
            request_id=str(meta.get("RequestId", "")),
            task_id=str(payload.get("TaskId", "")),
            dry_run=self.dry_run,
            payload=_mask(payload),
        )
        if not ok:
            code = error.get("Code") or f"HTTP_{status}"
            message = error.get("Message") or (data or {}).get("_raw", "")
            result.error = f"{code}: {message}"
            logger.warning("RTC %s 失败：%s", action, result.error)
        else:
            logger.info("RTC %s 成功：request_id=%s dry_run=%s", action, result.request_id, self.dry_run)
        return result

    # ------------------------------------------------------------------ #
    # 业务动作
    # ------------------------------------------------------------------ #
    def start_voice_chat(self, payload: dict[str, Any]) -> RtcCallResult:
        """把云端 Agent 拉进房间（Action=StartVoiceChat）。

        常见失败原因（按出现频率排序）：
            1. 同一房间已经有 Agent 在跑 —— 必须先把上一个挂断（StopVoiceChat）；
            2. 回调地址不可达 —— 云端 POST 不到我们的服务（ngrok 过期最常见）；
            3. AppId 与 AK/SK 不属于同一个账号 —— 签名能过但鉴权不过。
        """
        return self._call("StartVoiceChat", payload, timeout=15.0)

    def stop_voice_chat(self, *, app_id: str, room_id: str, task_id: str = "") -> RtcCallResult:
        """挂断房间里的 Agent（Action=StopVoiceChat）。"""
        payload: dict[str, Any] = {"AppId": app_id or self.settings.rtc_app_id, "RoomId": room_id}
        if task_id:
            payload["TaskId"] = task_id
        return self._call("StopVoiceChat", payload)

    def update_voice_chat(self, payload: dict[str, Any]) -> RtcCallResult:
        """运行中更新配置（换音色/改提示词热更新时用）。"""
        return self._call("UpdateVoiceChat", payload)

    # ------------------------------------------------------------------ #
    # 测试/演示辅助
    # ------------------------------------------------------------------ #
    def recorded_calls(self) -> list[dict[str, Any]]:
        """演练模式下已记录的调用（测试断言"下发了什么"用）。"""
        if isinstance(self.transport, RecordingTransport):
            return list(self.transport.calls)
        return []
