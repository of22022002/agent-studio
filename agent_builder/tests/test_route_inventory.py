# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""SUT-01 CD-018 (B2) §6.2: 路由全集快照校验。

加载 fixtures/b2_route_inventory.json，对照 live Flask url_map + FastAPI app.routes：
- 公开 Flask 路由（prompt/mmapo）不得丢失/被未经登记改动；
- 每个公开 Flask 路由的直连路径须被 dispatcher 覆盖（normalize → /flask 前缀）；
- /static 归 auto-static，dispatcher 不接管；
- FastAPI 公开路由集合稳定。

路由增删会令本测试失败 → 提示更新 fixture（§6.2"修改前后清单差异仅允许已批准变化"）。
"""
import json
import os

import pytest

from agent_builder.serve.common.flask_route_dispatch import normalize_registered_flask_path

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "b2_route_inventory.json")


def _load():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _live_flask_rules():
    from agent_builder.app import app as flask_app
    out = []
    for r in flask_app.url_map.iter_rules():
        methods = sorted(m for m in (r.methods or set()) if m not in ("HEAD", "OPTIONS"))
        out.append({"rule": r.rule, "methods": methods, "endpoint": r.endpoint})
    return out


def _live_fastapi_paths():
    from agent_builder.serve.server_fastapi import app as fa_app
    paths = set()
    stack = list(fa_app.routes)

    def walk(routes):
        for rt in routes:
            p = getattr(rt, "path", None)
            if p is not None and getattr(rt, "methods", None) is not None:
                paths.add(p)
            if hasattr(rt, "routes"):
                walk(rt.routes)
            orig = getattr(rt, "original_router", None)
            if orig is not None:
                walk(getattr(orig, "routes", []))

    walk(fa_app.routes)
    return paths


def test_flask_public_routes_match_snapshot():
    """公开 Flask 路由（prompt/mmapo）集合须与 fixture 一致；增删须更新 fixture。"""
    snap = {r["rule"]: r for r in _load()["flask_url_map"] if r["class"].startswith("public")}
    live = {r["rule"]: r for r in _live_flask_rules() if r["rule"].startswith("/flask/v1/")}
    assert set(snap) == set(live), (
        f"Flask public route set drifted from snapshot; "
        f"missing_in_live={set(snap)-set(live)} new_in_live={set(live)-set(snap)}; "
        f"update fixtures/b2_route_inventory.json if change is approved")
    for rule in snap:
        assert snap[rule]["methods"] == live[rule]["methods"], (
            f"methods drift on {rule}: snap={snap[rule]['methods']} live={live[rule]['methods']}")


def test_every_public_flask_route_covered_by_dispatcher():
    """每个公开 Flask 路由的直连路径（去 /flask 前缀）须被 dispatcher 覆盖。"""
    live = [r for r in _live_flask_rules() if r["rule"].startswith("/flask/v1/")]
    assert live, "no public Flask routes found"
    for r in live:
        direct = r["rule"][len("/flask"):]  # /flask/v1/prompt/build → /v1/prompt/build
        # 对带 <param> 的规则，用前缀（dispatcher 用 startswith 匹配前缀）
        target = normalize_registered_flask_path(direct)
        assert target is not None, (
            f"public Flask route {r['rule']} (direct {direct}) not covered by dispatcher")


def test_static_route_classified_and_excluded():
    """/static 为 auto-static，dispatcher 不接管（转 FastAPI）。"""
    snap_static = [r for r in _load()["flask_url_map"] if r["class"] == "auto-static"]
    assert any(r["rule"].startswith("/static") for r in snap_static)
    assert normalize_registered_flask_path("/static/foo.js") is None, \
        "/static must NOT be dispatched to Flask (auto rule, non-public)"


def test_fastapi_route_set_stable():
    """FastAPI 公开路由集合须与 fixture 一致（root mount 移除后 FastAPI 拥有的路径）。"""
    snap_paths = {r["path"] for r in _load()["fastapi_routes"]}
    live_paths = _live_fastapi_paths()
    assert snap_paths == live_paths, (
        f"FastAPI route set drifted; "
        f"missing={snap_paths-live_paths} new={live_paths-snap_paths}; "
        f"update fixture if approved")


def test_no_root_catch_all_mount():
    """root mount catch-all 已移除（dispatcher 接管）。"""
    from agent_builder.serve.server_fastapi import app as fa_app
    mounts = [r for r in fa_app.routes if getattr(r, "path", None) in ("/", "")]
    assert mounts == [], f"root catch-all mount should be removed, got {mounts}"
