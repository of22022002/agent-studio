"""Tests for the agent_builder FastAPI shell — route surface only.

Uses TestClient WITHOUT entering lifespan (no `with`), so startup hooks
(ContextManager set_store / Redis ping) do not run and no infra is required.
"""
from fastapi.testclient import TestClient

from agent_builder.serve.server_fastapi import app


def _client():
    # No `with` → lifespan does not run → no Redis/DB needed.
    return TestClient(app, raise_server_exceptions=False)


def _all_paths(routes):
    """Collect all route paths, recursing into FastAPI's _IncludedRouter
    wrappers. FastAPI 0.139+ wraps included routers (whose routes only appear
    under ``original_router``); older versions flatten them into ``app.routes``.
    Duck-typed via ``original_router`` so no private-class import is needed.
    """
    paths = set()
    stack = list(routes)
    while stack:
        r = stack.pop()
        p = getattr(r, "path", None)
        if p is not None:
            paths.add(p)
        orig = getattr(r, "original_router", None)
        if orig is not None:
            stack.extend(getattr(orig, "routes", []))
    return paths


def test_health_returns_200_without_lifespan():
    r = _client().get("/v1/health")
    assert r.status_code == 200
    assert r.text == "the health is good"


def test_health_returns_custom_rsp_when_configured(monkeypatch):
    from agent_builder.adapter.config_bridge import settings

    monkeypatch.setattr(settings.health_check, "custom_rsp", "custom-healthy")
    r = _client().get("/v1/health")
    assert r.status_code == 200
    assert r.text == "custom-healthy"


def test_n2l_route_is_registered():
    routes = _all_paths(app.routes)
    assert "/v1/{project_id}/{agent_type}/generator/conversations/{cid}/chat" in routes


def test_root_mount_removed_and_dispatcher_present():
    # SUT-01 CD-018 (B2): 根路径 WSGI catch-all 已移除；Flask app 由
    # FlaskRouteDispatchMiddleware 持有，不再 mount("/")。
    mounts = [r for r in app.routes if getattr(r, "path", None) in ("/", "")]
    assert mounts == [], f"root catch-all mount should be removed, got {mounts}"
    names = [
        (m.cls.__name__ if hasattr(m, "cls") else type(m).__name__)
        for m in app.user_middleware
    ]
    assert "FlaskRouteDispatchMiddleware" in names, (
        f"FlaskRouteDispatchMiddleware should be registered, got {names}")


def test_middleware_order_context_outer_dispatcher_inner():
    # §6.4: establish_inbound_context（@app.middleware→BaseHTTPMiddleware）必须在
    # FlaskRouteDispatchMiddleware 外层（先建立 ID 再分派）。LIFO：后注册者外层。
    names = [
        (m.cls.__name__ if hasattr(m, "cls") else type(m).__name__)
        for m in app.user_middleware
    ]
    try:
        ctx_idx = names.index("BaseHTTPMiddleware")
    except ValueError:
        ctx_idx = -1
    disp_idx = names.index("FlaskRouteDispatchMiddleware")
    assert ctx_idx >= 0 and ctx_idx < disp_idx, (
        f"context(establish) must be OUTER than dispatcher; got {names}")


def test_fastapi_path_wrong_method_returns_405_with_allow():
    # CD-018: DELETE /v1/health 原被 root mount 抢占为 Flask 404，现 FastAPI 拥有
    # method ownership → canonical 405 + Allow（13100003）。
    r = _client().delete("/v1/health", headers={"X-Request-Id": "rid-del-health"})
    assert r.status_code == 405
    assert r.headers.get("Allow") is not None
    assert r.json()["error_code"] == "openjiuwen.13100003"
    assert r.json()["request_id"] == "rid-del-health"
    assert r.headers.get("X-Request-Id") == "rid-del-health"


def test_fastapi_path_options_returns_405_with_allow():
    # §6.2/§8.3 连带：FastAPI 已登记路径 OPTIONS 由 FastAPI 返回 405+Allow
    # （恢复 method ownership 的同源变化，不通过 dispatcher 特判回退）。
    r = _client().options("/v1/health", headers={"X-Request-Id": "rid-opt-health"})
    assert r.status_code == 405
    assert r.headers.get("Allow") is not None
    assert r.json()["error_code"] == "openjiuwen.13100003"


def test_flask_path_options_keeps_method_limit_404():
    # §6.9: Flask 路径 OPTIONS 经 dispatcher 进入 Flask，保持 method_limit() 空 body + 404。
    r = _client().options("/v1/prompt/build", headers={"X-Request-Id": "rid-opt-prompt"})
    assert r.status_code == 404


def test_fastapi_unknown_path_returns_canonical_404():
    r = _client().get("/v1/__no_such_fastapi_route__", headers={"X-Request-Id": "rid-404"})
    assert r.status_code == 404
    assert r.json()["error_code"] == "openjiuwen.13100002"


def test_prompt_route_reaches_flask_not_fastapi_404():
    # /v1/prompt/build 经 dispatcher 规范化为 /flask/v1/prompt/build 命中 Flask；
    # 不应被 FastAPI 当未知路径 404。空 body → Flask 侧 4xx（非 FastAPI canonical 404/405）。
    r = _client().post("/v1/prompt/build", json={},
                       headers={"X-Request-Id": "rid-prompt"})
    assert not (r.status_code == 404 and r.json().get("error_code") == "openjiuwen.13100002"), (
        f"prompt route should reach Flask, not FastAPI 404; got {r.status_code} {r.text[:120]}")


def test_mmapo_route_reaches_flask():
    # /v1/MMprompt/... 经 dispatcher 规范化命中 Flask mmapo blueprint
    r = _client().post("/v1/MMprompt/templates_optimization/jobs", json={},
                       headers={"X-Request-Id": "rid-mmapo"})
    assert not (r.status_code == 404 and r.json().get("error_code") == "openjiuwen.13100002"), (
        f"mmapo route should reach Flask, not FastAPI 404; got {r.status_code} {r.text[:120]}")


def test_allow_whitelist_no_arbitrary_header_copy():
    # §6.5: 405 仅透传 Allow，不无条件复制 exc.headers；伪造头不得注入响应。
    # 用 FastAPI 路径错误 method 触发 405（Starlette HTTPException 带 headers）。
    r = _client().delete("/v1/health", headers={"X-Request-Id": "rid-allow"})
    assert r.status_code == 405
    assert "Allow" in r.headers
    # X-Request-Id 不被 Allow 逻辑覆盖
    assert r.headers.get("X-Request-Id") == "rid-allow"


def test_correlation_header_single_value_on_fastapi_path():
    r = _client().get("/v1/health", headers={"X-Request-Id": "rid-single"})
    assert r.headers.get_list("X-Request-Id") == ["rid-single"]


def test_query_string_preserved_through_dispatch():
    r = _client().get("/v1/health?foo=bar&baz=qux", headers={"X-Request-Id": "rid-q"})
    assert r.status_code == 200

