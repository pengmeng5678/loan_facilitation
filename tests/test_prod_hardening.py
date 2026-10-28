"""生产加固测试（对应生产就绪度评估里的两条 P0）。

    P0-4  /ready 把内部异常原文返回给调用方 → 响应体只回答"好/没好"
    P0-1  七个 API 前缀零鉴权             → default-deny 中间件 + 显式白名单

鉴权测试的做法：现场 `create_app()` 一个开了鉴权的应用（monkeypatch 掉
`agent_platform.api.app.get_settings`），再用夹具上下文替换 app_context 单例
—— 不污染模块级 `app`（默认关闭鉴权，其他 API 测试照旧无 key 访问）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import app as app_module
from agent_platform.api.app import app
from agent_platform.api.auth import ApiKeyAuth
from agent_platform.config import Settings

#: 中间件 401 的固定文案（用来区分"被中间件挡了"和"被业务自己的鉴权挡了"）
_MIDDLEWARE_401_DETAIL = "缺少或无效的 API Key"


@pytest.fixture
def client(app_context_fixture) -> TestClient:
    """模块级 app（默认不开鉴权）+ 夹具上下文。"""
    return TestClient(app)


# =========================================================================== #
# 辅助：现场构建"开了鉴权"的应用
# =========================================================================== #
def _authed_client(monkeypatch, app_context_fixture, **overrides) -> TestClient:
    """构建一个开了 API 鉴权的 FastAPI 应用并返回测试客户端。

    * monkeypatch `agent_platform.api.app.get_settings` —— create_app() 从
      模块级名字取配置，换掉它即可让同一个工厂产出不同配置的应用；
    * monkeypatch `app_context.get_app_context` —— 请求进来时各路由都走
      这个入口拿服务实例，指向夹具上下文，测试就不碰真实数据目录。
    """
    import app_context as ac

    monkeypatch.setattr(ac, "get_app_context", lambda: app_context_fixture)
    settings = Settings(
        app_env="local",
        log_level="WARNING",
        llm_provider="echo",
        embedding_provider="local",
        embedding_dim=256,
        checkpointer_backend="memory",
        vector_backend="local",
        api_auth_enabled=True,
        **overrides,
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    return TestClient(app_module.create_app())


# =========================================================================== #
# P0-4：/ready 不许泄露内部异常
# =========================================================================== #
def test_ready_does_not_leak_internal_errors(client, monkeypatch):
    """装配炸了的时候，/ready 只许说"没好"，不许把异常原文贴到响应体上。

    背景：原实现返回 `{"error": f"{type}: {msg}"}`，而 /ready 是探针地址，
    异常文本经常带内部路径、依赖名、SQL —— 等于免费情报。
    """
    import app_context as ac

    secret = "D:/internal/secret/business.sqlite"

    def _boom():
        raise RuntimeError(f"连接数据库失败：{secret}")

    monkeypatch.setattr(ac, "get_app_context", _boom)

    resp = client.get("/ready")
    assert resp.status_code == 200          # 探针语义：服务活着，只是没就绪
    assert resp.json() == {"status": "not_ready"}
    assert secret not in resp.text
    assert "RuntimeError" not in resp.text
    assert "连接数据库失败" not in resp.text


# =========================================================================== #
# P0-1：鉴权中间件（纯逻辑）
# =========================================================================== #
class TestApiKeyAuthUnit:
    """不碰 HTTP，直接验密钥校验器本体。"""

    def test_verify_multiple_keys(self):
        auth = ApiKeyAuth.from_settings(
            Settings(api_auth_enabled=True, api_auth_keys="key-alpha, key-beta ")
        )
        assert auth.enabled is True
        assert auth.verify("key-alpha") is True
        assert auth.verify("key-beta") is True      # 逗号分隔 + 空格容忍
        assert auth.verify("key-gamma") is False
        assert auth.verify(None) is False
        assert auth.verify("") is False

    def test_empty_key_set_is_disabled(self):
        auth = ApiKeyAuth.from_settings(Settings(api_auth_enabled=True))
        assert auth.enabled is False
        assert auth.verify("anything") is False     # 空集合 = 谁都过不了

    def test_whitelist_and_self_auth_prefixes(self):
        auth = ApiKeyAuth(frozenset({"k"}))
        # 白名单：探针 / 文档 / 静态资源
        for path in ("/health", "/ready", "/", "/docs", "/openapi.json", "/redoc"):
            assert auth.is_open(path) is True, path
        assert auth.is_open("/static/swagger-ui/swagger-ui-bundle.js") is True
        # 自带鉴权的语音回调前缀
        assert auth.is_open("/voice/callback/chat") is True
        # 其余一律要 key —— 包括同前缀下的普通业务路径
        assert auth.is_open("/voice/scenes") is False
        assert auth.is_open("/flow/leads") is False
        assert auth.is_open("/platform/agents") is False

    def test_install_returns_none_when_disabled(self):
        """没开鉴权时不装中间件（本地/测试路径零行为变化）。"""
        import agent_platform.api.auth as auth_module

        # 模块级 app 默认没开鉴权；对已构建好的 app 再 install 一次也只是短路返回
        result = auth_module.install_api_key_auth(app, Settings(api_auth_enabled=False))
        assert result is None


# =========================================================================== #
# P0-1：鉴权中间件（HTTP 端到端）
# =========================================================================== #
def test_auth_disabled_by_default(client):
    """默认关闭：不带任何 key，业务路径直接放行（本地开发体验优先）。"""
    resp = client.get("/flow/leads")
    assert resp.status_code == 200


def test_auth_rejects_missing_and_wrong_key(monkeypatch, app_context_fixture):
    c = _authed_client(monkeypatch, app_context_fixture, api_auth_keys="secret-key")

    missing = c.get("/flow/leads")
    assert missing.status_code == 401
    assert missing.json()["detail"] == _MIDDLEWARE_401_DETAIL
    assert "WWW-Authenticate" in missing.headers

    wrong = c.get("/flow/leads", headers={"X-API-Key": "wrong-key"})
    assert wrong.status_code == 401
    # 两种失败一个文案：不告诉调用方"是没带还是带错了"
    assert wrong.json()["detail"] == _MIDDLEWARE_401_DETAIL


def test_auth_accepts_x_api_key_and_bearer(monkeypatch, app_context_fixture):
    c = _authed_client(monkeypatch, app_context_fixture, api_auth_keys="secret-key")

    via_header = c.get("/flow/leads", headers={"X-API-Key": "secret-key"})
    assert via_header.status_code == 200

    via_bearer = c.get(
        "/flow/leads", headers={"Authorization": "Bearer secret-key"}
    )
    assert via_bearer.status_code == 200

    # POST 同样被保护（不只是 GET）
    posted = c.post(
        "/flow/sop/sweep", headers={"X-API-Key": "secret-key"}
    )
    assert posted.status_code == 200


def test_auth_key_rotation_two_keys(monkeypatch, app_context_fixture):
    """轮换期两把 key 同时有效（新 key 先上、旧 key 后下）。"""
    c = _authed_client(monkeypatch, app_context_fixture, api_auth_keys="old,new")

    assert c.get("/flow/leads", headers={"X-API-Key": "old"}).status_code == 200
    assert c.get("/flow/leads", headers={"X-API-Key": "new"}).status_code == 200
    assert c.get("/flow/leads", headers={"X-API-Key": "older"}).status_code == 401


def test_auth_whitelist_paths_stay_open(monkeypatch, app_context_fixture):
    c = _authed_client(monkeypatch, app_context_fixture, api_auth_keys="secret-key")

    # 探针：要 key 等于没有探针
    assert c.get("/health").status_code == 200
    assert c.get("/ready").json()["status"] == "ready"
    # 文档与静态资源：反代层再收紧，这里保调试体验
    assert c.get("/").status_code == 200
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/docs").status_code == 200
    assert c.get("/static/swagger-ui/swagger-ui.css").status_code == 200


def test_auth_voice_callback_prefix_exempt(monkeypatch, app_context_fixture):
    """/voice/callback/* 不走中间件 —— 它有独立更强的鉴权（回调专用 key）。

    对照项：同前缀的 /voice/scenes 是普通业务路径，必须 401。
    """
    c = _authed_client(monkeypatch, app_context_fixture, api_auth_keys="secret-key")

    business = c.get("/voice/scenes")
    assert business.status_code == 401
    assert business.json()["detail"] == _MIDDLEWARE_401_DETAIL

    # 回调不带 key：不被中间件拦（由路由自己的 VOICE_CALLBACK_API_KEY 把关；
    # 测试环境未配那个 key，voice 服务按设计放行并打 warning）
    resp = c.post(
        "/voice/callback/chat",
        json={"messages": [{"role": "user", "content": "利率多少"}]},
    )
    assert _MIDDLEWARE_401_DETAIL not in resp.text


def test_auth_fail_closed_when_keys_missing(monkeypatch, app_context_fixture):
    """开了开关却忘了配密钥 → 全部 401，而不是"当作没开"悄悄裸奔。"""
    c = _authed_client(monkeypatch, app_context_fixture, api_auth_keys="")

    resp = c.get("/flow/leads")
    assert resp.status_code == 401
    # 探针仍然可用（否则 K8s 会把服务判死，问题被掩盖得更深）
    assert c.get("/health").status_code == 200
