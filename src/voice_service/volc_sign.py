"""火山引擎 OpenAPI 的 V4 签名。

为什么要自己实现：火山引擎的 OpenAPI（RTC 的 StartVoiceChat 等）要求请求带
`Authorization: HMAC-SHA256 Credential=...` 的签名头，官方 Python SDK 覆盖不全
（RTC 的 AIGC 接口经常要手写），而且为了少一个重依赖，这里按官方算法实现一遍。

算法（与 AWS SigV4 同构，火山官方文档的命名一致）：

    CanonicalRequest = Method \n CanonicalURI \n CanonicalQueryString \n
                       CanonicalHeaders \n SignedHeaders \n Hex(SHA256(Body))

    StringToSign     = "HMAC-SHA256" \n XDate \n CredentialScope \n
                       Hex(SHA256(CanonicalRequest))

    SigningKey       = HMAC(HMAC(HMAC(HMAC(SecretKey, date), region), service), "request")
    Signature        = Hex(HMAC(SigningKey, StringToSign))

    CredentialScope  = date/region/service/request

三个最容易错的地方（线上排障时也按这个顺序查）：
    1. `CanonicalQueryString` 必须**按 key 字典序**编码，且 value 要 URL 编码；
    2. `SignedHeaders` 里的 header 名必须**小写**且与 CanonicalHeaders 一致；
    3. `X-Date` 与 CredentialScope 里的 date 必须是**同一个 UTC 日期**，
       跨零点签出来的请求会直接被拒（报签名不匹配，但其实是日期不一致）。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

ALGORITHM = "HMAC-SHA256"
SERVICE_REQUEST = "request"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value: str) -> str:
    """RFC3986 编码：`/` 与 `~` 不转义，其余按 utf-8 百分号编码。"""
    return quote(value, safe="-_.~")


def canonical_query_string(params: dict[str, str]) -> str:
    """按 key 字典序拼查询串（火山与 AWS 都要求排序，否则签名对不上）。"""
    if not params:
        return ""
    return "&".join(f"{_uri_encode(k)}={_uri_encode(str(v))}" for k, v in sorted(params.items()))


@dataclass(frozen=True)
class VolcCredentials:
    access_key: str
    secret_key: str

    @property
    def configured(self) -> bool:
        return bool(self.access_key and self.secret_key)


def sign_request(
    *,
    credentials: VolcCredentials,
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    body: bytes,
    region: str,
    service: str,
    x_date: str | None = None,
) -> dict[str, str]:
    """生成签名所需的请求头。

    返回：`{"X-Date": ..., "Authorization": ..., "X-Content-Sha256": ...}`，
    调用方只需再补 `Host` / `Content-Type`。
    """
    now = datetime.now(UTC)
    x_date = x_date or now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = x_date[:8]  # YYYYMMDD

    payload_hash = _sha256_hex(body or b"")

    # 参与签名的头（全部小写；Host 必须带端口时也照原样）
    headers = {
        "content-type": "application/json",
        "host": host,
        "x-content-sha256": payload_hash,
        "x-date": x_date,
    }
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))

    canonical_request = "\n".join(
        [
            method.upper(),
            path or "/",
            canonical_query_string(query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    scope = f"{date_stamp}/{region}/{service}/{SERVICE_REQUEST}"
    string_to_sign = "\n".join([ALGORITHM, x_date, scope, _sha256_hex(canonical_request.encode("utf-8"))])

    key = _hmac(credentials.secret_key.encode("utf-8"), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, SERVICE_REQUEST)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{ALGORITHM} Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }
