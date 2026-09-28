# -*- coding: UTF-8 -*-
"""global_memory_ref_resolution_patch 单元测试。

验证 CommitState.get_inputs 对 ${MEMORY_VARIABLE.xxx} 引用的 global_state 兜底：
- 循环体内写入的记忆变量，循环外节点输入解析可取到（核心缺陷场景）
- 未命中时维持原结果，不影响未执行分支保护
- 非记忆变量引用行为零变化
- io_state 已有实际值时不覆盖（fill-when-missing）
"""

import pytest

from jiuwen.extension.patches.global_memory_ref_resolution_patch import (
    GLOBAL_REF_PREFIX,
    _resolve_memory_leaves,
    apply_global_memory_ref_resolution_patch,
)

assert apply_global_memory_ref_resolution_patch(), "补丁应在模块导入时应用一次"

from openjiuwen.core.session import NodeSession, SubWorkflowSession, WorkflowSession
from openjiuwen.core.session.state.workflow_state import InMemoryState

GLOBAL_REF = "${" + GLOBAL_REF_PREFIX + "mem_counter}"


def _build_loop_scenario():
    """还原真实执行会话层级：工作流 → 循环节点 → SubWorkflowSession → 循环体。

    返回 (wf_session, sub_wf_session)。
    """
    wf_session = WorkflowSession(workflow_id="main_wf", parent=None)
    wf_session._state = InMemoryState()  # pylint: disable=protected-access
    loop_node_session = NodeSession(wf_session, "node_loop_1")
    sub_wf_session = SubWorkflowSession(loop_node_session, "loop_body_wf")
    return wf_session, sub_wf_session


def _write_memory_inside_loop(sub_wf_session, var_name, value):
    """模拟循环体内 LoopSetVariable 写记忆变量（root_session.update_global）。"""
    set_var_inner = NodeSession(sub_wf_session, "node_set_var")
    root_session = set_var_inner.parent()
    root_session.state().update_global({f"{GLOBAL_REF_PREFIX}{var_name}": value})
    root_session.state().commit()


def test_memory_ref_resolves_outside_loop():
    """核心场景：循环内写入 → 循环外 get_inputs 解析到值（修复前为 None）。"""
    wf_session, sub_wf_session = _build_loop_scenario()
    _write_memory_inside_loop(sub_wf_session, "mem_counter", "loop_value_1")

    after_loop = NodeSession(wf_session, "node_after_loop")
    schema = {"userFields": {"result": GLOBAL_REF}}
    resolved = after_loop.state().get_inputs(schema)
    assert resolved["userFields"]["result"] == "loop_value_1"


def test_end_node_style_ref_not_blank():
    """End 节点 #end_ 前缀映射场景：引用可解析，不再被 sanitize 置空的前提成立。"""
    wf_session, sub_wf_session = _build_loop_scenario()
    _write_memory_inside_loop(sub_wf_session, "answer", "from_loop")

    end_node = NodeSession(wf_session, "node_end")
    schema = {"userFields": {"#end_result": "${" + GLOBAL_REF_PREFIX + "answer}"}}
    resolved = end_node.state().get_inputs(schema)
    assert resolved["userFields"]["#end_result"] == "from_loop"


def test_unresolved_memory_ref_stays_none():
    """global_state 也无该变量时维持 None（交由既有保护逻辑，不新增行为）。"""
    wf_session, _ = _build_loop_scenario()
    node = NodeSession(wf_session, "node_x")
    schema = {"userFields": {"result": "${" + GLOBAL_REF_PREFIX + "never_set}"}}
    resolved = node.state().get_inputs(schema)
    assert resolved["userFields"]["result"] is None


def test_non_memory_refs_unaffected():
    """非 MEMORY_VARIABLE 引用仍走 io_state，行为零变化。"""
    wf_session, _ = _build_loop_scenario()
    start = NodeSession(wf_session, "node_start")
    start.state().set_outputs({"systemFields": {"query": "hello"}})
    start.state().commit()

    node = NodeSession(wf_session, "node_y")
    schema = {"userFields": {"q": "${node_start.systemFields.query}"}}
    resolved = node.state().get_inputs(schema)
    assert resolved["userFields"]["q"] == "hello"

    # io_state 中不存在的节点引用仍为 None（不会被误兜底）
    schema2 = {"userFields": {"x": "${node_missing.out}"}}
    resolved2 = node.state().get_inputs(schema2)
    assert resolved2["userFields"]["x"] is None


def test_io_state_value_not_overridden():
    """io_state 已解析出实际值时不覆盖（fill-when-missing）。"""
    wf_session, _ = _build_loop_scenario()
    # global_state 写入该记忆变量
    seed = NodeSession(wf_session, "node_seed")
    seed.state().update_global({f"{GLOBAL_REF_PREFIX}dup": "from_global"})
    seed.state().commit()
    # 向 io_state 写入同路径的值（模拟异常场景）
    io_state = wf_session.state()._io_state  # pylint: disable=protected-access
    io_state.update_by_id_and_commit("MEMORY_VARIABLE", {"MEMORY_VARIABLE": {"dup": "from_io"}})

    node = NodeSession(wf_session, "node_z")
    schema = {"userFields": {"v": "${" + GLOBAL_REF_PREFIX + "dup}"}}
    resolved = node.state().get_inputs(schema)
    assert resolved["userFields"]["v"] == "from_io"


def test_string_schema_memory_ref():
    """schema 为纯字符串引用时同样兜底。"""
    wf_session, sub_wf_session = _build_loop_scenario()
    _write_memory_inside_loop(sub_wf_session, "plain", "plain_value")

    node = NodeSession(wf_session, "node_s")
    resolved = node.state().get_inputs(GLOBAL_REF)
    assert resolved == "plain_value"


def test_list_schema_memory_ref():
    """schema 为列表结构时逐项兜底。"""
    wf_session, sub_wf_session = _build_loop_scenario()
    _write_memory_inside_loop(sub_wf_session, "list_var", [1, 2])

    node = NodeSession(wf_session, "node_l")
    schema = {"items": ["${" + GLOBAL_REF_PREFIX + "list_var}"]}
    resolved = node.state().get_inputs(schema)
    assert resolved["items"] == [[1, 2]]


def test_patch_apply_idempotent():
    """重复 apply 返回 False，不重复包装。"""
    assert apply_global_memory_ref_resolution_patch() is False


def test_resolve_memory_leaves_cow_no_hit():
    """无命中时返回原对象（COW 零拷贝路径）。"""
    schema = {"a": {"b": "${node_x.y}"}}
    result = {"a": {"b": None}}
    assert _resolve_memory_leaves(schema, result, None) is result


def test_resolve_memory_leaves_mismatched_shapes():
    """schema 与 result 结构不一致时安全返回原 result。"""
    assert _resolve_memory_leaves({"a": 1}, "not-a-dict", None) == "not-a-dict"
    assert _resolve_memory_leaves(["x"], "not-a-list", None) == "not-a-list"
