# -*- coding: utf-8 -*-
"""本地 CodeCheck 自检脚本（提交前增量自检）。

背景：openJiuwen CI 使用 openlibing CodeCheck 服务（规则集在服务端配置，
本地无法获取全集）。本脚本覆盖本仓库已实证命中的规则子集，做提交前
增量自检，减少 CI 往返。

已实证规则（来自本仓库 CodeCheck 结果与代码内注释）：
  G.CTL.03  条件/循环控制语句中布尔操作数不超过 3 个。
            规避范式：提取独立判定函数（参见 end.py:_has_response_payload、
            loop-modal.component.ts:isNumericStringLiteral）。
            引擎识别 `# noqa: G.CTL.03` 抑制，但仓库惯例是规避而非抑制。
  G.FNM.01  函数默认参数不得为可变对象（[]/dict()/set() 等）。
  G.FNM.02  闭包不得捕获循环变量（AST 难以精确判定，本脚本不覆盖）。
  G.FNM.03  函数参数过多（约 >5 个），规避：封装参数对象/参数组。
  G.ERR.07  except Exception 需 `# noqa: G.ERR.07` 显式标注（存量惯例）。
  G.ERR.13 / G.LOG.02 / G.COM.08 / G.VAR.03  错误契约/日志/注释/重复声明，
            多为设计约定类，本脚本不覆盖，人工按注释规范执行。

用法：
  python tools/codecheck_self.py                        # 增量：相对 merge-base 的变更文件
  python tools/codecheck_self.py --base origin/studio-2.0-dev
  python tools/codecheck_self.py path1.py path2.ts ...   # 显式指定文件

退出码：存在违规时为 1，否则 0（可挂 pre-commit）。
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE = "upstream/studio-2.0-dev"
MAX_BOOL_OPERANDS = 3

FINDINGS: list[str] = []


def report(rule: str, path: str, line: int, message: str) -> None:
    FINDINGS.append(f"{rule} · {path}:{line} · {message}")


# ---------------------------------------------------------------- Python AST

def _boolop_leaf_count(node: ast.AST) -> int:
    """统计条件表达式中布尔操作数个数（BoolOp 树展平后的叶子数）。"""
    if isinstance(node, ast.BoolOp):
        return sum(_boolop_leaf_count(v) for v in node.values)
    return 1


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source_lines: list[str]) -> None:
        self.path = path
        self.lines = source_lines

    def _flag_boolop(self, node: ast.expr) -> None:
        count = _boolop_leaf_count(node)
        if count > MAX_BOOL_OPERANDS:
            line_text = (
                self.lines[node.lineno - 1] if node.lineno <= len(self.lines) else ""
            )
            if "noqa" in line_text:  # 显式抑制
                return
            report(
                "G.CTL.03", self.path, node.lineno,
                f"条件中布尔操作数 {count}/{MAX_BOOL_OPERANDS}，"
                "提取独立判定函数规避",
            )

    def _flag_mutable_default(self, node) -> None:
        defaults = [
            d for d in (node.args.defaults + node.args.kw_defaults) if d is not None
        ]
        for d in defaults:
            bad = isinstance(d, (ast.List, ast.Dict, ast.Set)) or (
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Name)
                and d.func.id in {"list", "dict", "set"}
            )
            if bad:
                report(
                    "G.FNM.01", self.path, node.lineno,
                    "函数默认参数为可变对象，改用 None + 函数内创建",
                )

    def visit_If(self, node: ast.If) -> None:
        self._flag_boolop(node.test)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._flag_boolop(node.test)
        self.generic_visit(node)

    def visit_FunctionDef(self, node) -> None:
        self._flag_mutable_default(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node) -> None:
        self._flag_mutable_default(node)
        self.generic_visit(node)


def check_python(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        report("SYNTAX", str(path), e.lineno or 0, f"语法错误: {e.msg}")
        return
    _PyVisitor(str(path), source.splitlines()).visit(tree)


# ---------------------------------------------------------------- TS 启发式

_TS_IF_SPAN = re.compile(
    r"\b(?:if|while)\s*\(([^{}]*?)\)\s*(?:\{|=>|\{)", re.DOTALL
)


def check_typescript(path: Path) -> None:
    """启发式：if/while 条件内 &&/|| 操作符数 >= 3（即操作数 >= 4）时报 G.CTL.03。

    近似规则：忽略字符串字面量内的误报场景依赖人工复核；多行条件以
    `)` + `{` 边界归并。
    """
    text = path.read_text(encoding="utf-8")
    for m in _TS_IF_SPAN.finditer(text):
        condition = m.group(1)
        operators = len(re.findall(r"&&|\|\|", condition))
        if operators >= MAX_BOOL_OPERANDS:  # 3 个操作符 → 4 个操作数
            line = text[: m.start()].count("\n") + 1
            report(
                "G.CTL.03", str(path), line,
                f"条件中布尔操作符 {operators} 个（操作数 {operators + 1}），"
                "提取独立判定函数规避",
            )


# ---------------------------------------------------------------- 入口

def changed_files(base: str) -> list[Path]:
    merge_base = subprocess.run(
        ["git", "merge-base", base, "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    out = subprocess.run(
        ["git", "diff", "--name-only", merge_base, "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return [Path(p) for p in out if p.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 CodeCheck 自检（规则子集）")
    parser.add_argument("paths", nargs="*", help="显式指定待检文件（省略则增量）")
    parser.add_argument("--base", default=DEFAULT_BASE, help="增量基线分支")
    args = parser.parse_args()

    if args.paths:
        files = [Path(p) for p in args.paths]
    else:
        files = changed_files(args.base)

    for path in files:
        if not path.exists():
            continue
        if path.suffix == ".py":
            check_python(path)
        elif path.suffix in {".ts", ".tsx"}:
            check_typescript(path)

    if FINDINGS:
        print(f"发现 {len(FINDINGS)} 处违规：")
        for f in FINDINGS:
            print(f"  {f}")
        return 1
    print(f"自检通过：{len(files)} 个变更文件，无命中规则子集。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
