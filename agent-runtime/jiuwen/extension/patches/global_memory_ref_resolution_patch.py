# coding: utf-8

"""
Global memory variable reference resolution (jiuwen-side patch, no openjiuwen core edits).

问题：全局记忆变量写入 global_state（Start 播种、SetVariable/LoopSetVariable 更新），
但引擎为下游节点解析输入引用时（Vertex._pre_invoke → CommitState.get_inputs）
只查询 io_state，不查询 global_state。导致：

1. ${MEMORY_VARIABLE.xxx} 引用在 io_state 中无对应键，解析为 None；
   End 节点的未执行分支保护（_sanitize_leaf）随后将其静默替换为空串。
2. 循环体内写入的记忆变量（全局仓已更新），循环外引用拿到空值或陈旧默认值。

修复：monkey-patch CommitState.get_inputs。原有 io_state 解析逻辑保持不变；
仅对值为 `${MEMORY_VARIABLE.` 前缀引用的叶子节点，在解析未命中
（None 或原样保留的引用串）时，从 global_state 兜底取值；io_state 已解析出
实际值的场景不覆盖（fill-when-missing，最小影响面）。

边界：
- 仅影响 MEMORY_VARIABLE 前缀引用，其他引用行为零变化。
- global_state 也没有该值时维持原结果（None/引用串），交由既有保护逻辑处理。
- TODO: openjiuwen 内核官方支持 global refs 解析后移除本补丁（需 >= 修复版本）。

Applied once at import from ir_converter / sub_workflow.
"""

from __future__ import annotations

from typing import Any

# ${MEMORY_VARIABLE.xxx} 引用前缀（与 loop_set_variable.py / sub_workflow.py 一致）
GLOBAL_REF_PREFIX = "MEMORY_VARIABLE."


def _extract_memory_ref(value: Any) -> str | None:
    """若 value 是 `${MEMORY_VARIABLE.xxx}` 形式的引用串，返回 origin key，否则 None。"""
    if not isinstance(value, str):
        return None
    if not (value.startswith("${") and value.endswith("}")):
        return None
    origin = value[2:-1]
    if origin.startswith(GLOBAL_REF_PREFIX) and len(origin) > len(GLOBAL_REF_PREFIX):
        return origin
    return None


def _global_get(global_state: Any, key: str) -> Any:
    """从 global_state（InMemoryCommitState）按嵌套路径取值；不可用时返回 None。"""
    if global_state is None:
        return None
    try:
        return global_state.get(key)
    except Exception:  # 兜底路径不允许影响主解析流程
        return None


def _is_unresolved(value: Any, schema_leaf: str) -> bool:
    """判断叶子解析结果是否视为"未命中"：None 或原样保留的引用串。"""
    if value is None:
        return True
    return isinstance(value, str) and value == schema_leaf


def _resolve_memory_leaves(schema: Any, result: Any, global_state: Any) -> Any:
    """按 schema 结构遍历 result，对 MEMORY_VARIABLE 引用叶子做 global_state 兜底。

    COW：仅在实际命中替换时沿路径浅拷贝容器；未命中的子树保持原引用，
    零拷贝零分配。schema 与 result 结构一一对应（均由 get_by_schema 产出）。
    """
    if isinstance(schema, str):
        origin = _extract_memory_ref(schema)
        if origin is not None and _is_unresolved(result, schema):
            value = _global_get(global_state, origin)
            if value is not None:
                return value
        return result

    if isinstance(schema, dict):
        if not isinstance(result, dict):
            return result
        new: dict | None = None
        for key, sub_schema in schema.items():
            sub_result = result.get(key)
            patched = _resolve_memory_leaves(sub_schema, sub_result, global_state)
            if patched is not sub_result:
                if new is None:
                    new = dict(result)
                new[key] = patched
        return result if new is None else new

    if isinstance(schema, list):
        if not isinstance(result, list):
            return result
        new_list: list | None = None
        for i, sub_schema in enumerate(schema):
            sub_result = result[i] if i < len(result) else None
            patched = _resolve_memory_leaves(sub_schema, sub_result, global_state)
            if patched is not sub_result:
                if new_list is None:
                    new_list = list(result)
                new_list[i] = patched
        return result if new_list is None else new_list

    return result


_orig_commit_state_get_inputs = None
_PATCH_APPLIED = False


def _patched_commit_state_get_inputs(
        self, schema: str | list | dict | None = None) -> Any:
    """CommitState.get_inputs 的增强版：MEMORY_VARIABLE 引用 global_state 兜底。"""
    result = _orig_commit_state_get_inputs(self, schema)
    if schema is None or not isinstance(schema, (dict, list, str)):
        return result
    global_state = getattr(self, "_global_state", None)
    if global_state is None:
        return result
    return _resolve_memory_leaves(schema, result, global_state)


def apply_global_memory_ref_resolution_patch() -> bool:
    """Patch CommitState.get_inputs to resolve MEMORY_VARIABLE refs from global_state."""
    global _PATCH_APPLIED, _orig_commit_state_get_inputs
    if _PATCH_APPLIED:
        return False

    from openjiuwen.core.session.state.workflow_state import CommitState

    _orig_commit_state_get_inputs = CommitState.get_inputs
    CommitState.get_inputs = _patched_commit_state_get_inputs

    _PATCH_APPLIED = True
    return True
