"""新人上手校验：验证"代码位置"类答案里的锚点是否真实存在。

三种用法：

    # 1) 校验文档里的所有锚点（维护者用：文档说谎会立刻暴露）
    python scripts/onboarding_check.py --map docs/10-code-map.md

    # 2) 校验新人提交的答案（每行一个锚点，# 开头为注释）
    python scripts/onboarding_check.py --answer answers.txt

    # 3) 反查：这个符号在哪个文件（新人最常用）
    python scripts/onboarding_check.py --probe ALLOWED_TRANSITIONS

锚点格式：``路径::符号`` 或 ``路径::类::方法``，例如

    src/flow_ops/leads.py::ALLOWED_TRANSITIONS
    src/flow_ops/fees.py::FeeEstimator
    src/agent_platform/config.py::Settings::flow_high_risk_action_set
    web/src/hooks.ts::stageLabel

为什么用符号名而不是行号：行号在代码演化一天后就失效，符号名不会。
本脚本的存在意义就是让"文档/答案里的代码位置"可被机器验证——
文档说谎比没有文档更糟。

零第三方依赖（只用标准库），新人环境刚搭好就能跑。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 在源码/前端里搜符号时跳过这些目录
SKIP_DIRS = {"__pycache__", "node_modules", ".git", "dist", ".venv", "venv", ".data"}

# 锚点正则：路径::符号（::符号）*
# 路径允许字母数字 / . _ -，扩展名为 py/ts/tsx/css/md
ANCHOR_RE = re.compile(
    r"`?((?:[\w.-]+/)*[\w.-]+\.(?:py|ts|tsx|css))::([\w.]+(?:::[\w.]+)*)`?"
)

# 反查时"这是不是一个定义"的判定模式
DEF_PATTERNS = (
    re.compile(r"^\s*class\s+{name}\b"),
    re.compile(r"^\s*(?:async\s+)?def\s+{name}\s*\("),
    re.compile(r"^\s*{name}\s*(?::\s*[^=]+)?="),
    re.compile(r"^\s*{name}\s*="),
)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def read_lines(path: Path) -> list[str]:
    """读文件行；读不出来返回空列表（调用方负责判存在性）。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def symbol_defined(lines: list[str], name: str, *, within: str | None = None) -> bool:
    """判断 name 是否在 lines 里有定义。

    within 不为 None 时，只在 class/def within 的块内找（按缩进近似）。
    """
    start, end = 0, len(lines)
    if within:
        start, end = _class_span(lines, within)
        if start < 0:
            return False

    compiled = [p.pattern.format(name=re.escape(name)) for p in DEF_PATTERNS]
    compiled = [re.compile(p) for p in compiled]
    return any(p.search(line) for line in lines[start:end] for p in compiled)


def resolve_path(rel: str) -> tuple[Path | None, str]:
    """把文档里写的路径解析成真实文件。

    依次尝试：仓库根 → web/ 下 → 按 basename 在 src/ 下唯一匹配。
    第三种是为了容忍 `leads.py::X` 这种不带目录的简写（同一文件名在 src/ 下唯一时）。
    返回 (路径或 None, 说明)；说明为空表示原样命中。
    """
    direct = REPO_ROOT / rel
    if direct.exists():
        return direct, ""
    under_web = REPO_ROOT / "web" / rel
    if under_web.exists():
        return under_web, f"（在 web/ 下，建议写全 web/{rel}）"

    # 简写：按文件名在 src/ 下找，唯一才认
    if "/" not in rel:
        base = REPO_ROOT / "src"
        matches = [
            p
            for p in base.rglob(rel)
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
        ]
        if len(matches) == 1:
            return matches[0], f"（简写 {rel} → {matches[0].relative_to(REPO_ROOT).as_posix()}）"
        if len(matches) > 1:
            names = "、".join(m.relative_to(REPO_ROOT).as_posix() for m in matches[:4])
            return None, f"{rel} 在 src/ 下有 {len(matches)} 个同名文件（{names}），请写全路径"
        return None, f"文件不存在：{rel}"

    # 带了部分目录但缺包名（如 domain/matching.py）：按路径后缀唯一匹配
    base = REPO_ROOT / "src"
    matches = [
        p
        for p in base.rglob("*" + rel)
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
    ]
    if len(matches) == 1:
        return matches[0], f"（简写 {rel} → {matches[0].relative_to(REPO_ROOT).as_posix()}）"
    if len(matches) > 1:
        names = "、".join(m.relative_to(REPO_ROOT).as_posix() for m in matches[:4])
        return None, f"{rel} 匹配到 {len(matches)} 个文件（{names}），请写全路径"
    return None, f"文件不存在：{rel}"


def _class_span(lines: list[str], class_name: str) -> tuple[int, int]:
    """粗略找出 class class_name 的行范围（按缩进判断块结束）。找不到返回 (-1, -1)。"""
    head = re.compile(rf"^class\s+{re.escape(class_name)}\b")
    for index, line in enumerate(lines):
        if head.match(line):
            indent = len(line) - len(line.lstrip())
            for tail in range(index + 1, len(lines)):
                candidate = lines[tail]
                if not candidate.strip():
                    continue
                if (len(candidate) - len(candidate.lstrip())) <= indent:
                    return index, tail
            return index, len(lines)
    return -1, -1


def find_everywhere(symbol: str) -> list[tuple[str, int]]:
    """全仓库搜符号的定义位置，返回 [(相对路径, 行号)]，最多 8 条。"""
    hits: list[tuple[str, int]] = []
    patterns = [p.pattern.format(name=re.escape(symbol)) for p in DEF_PATTERNS]
    compiled = [re.compile(p) for p in patterns]
    for base in (REPO_ROOT / "src", REPO_ROOT / "web" / "src"):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".css"}:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            for lineno, line in enumerate(read_lines(path), start=1):
                if any(p.search(line) for p in compiled):
                    hits.append((path.relative_to(REPO_ROOT).as_posix(), lineno))
                    if len(hits) >= 8:
                        return hits
    return hits


# --------------------------------------------------------------------------- #
# 单点校验
# --------------------------------------------------------------------------- #
def check_anchor(anchor: str) -> tuple[bool, str]:
    """校验一个锚点，返回 (是否通过, 说明)。"""
    matched = ANCHOR_RE.search(anchor)
    if not matched:
        return False, "格式不认识，应为 路径::符号（如 src/flow_ops/leads.py::ALLOWED_TRANSITIONS）"

    rel, symbol_path = matched.group(1), matched.group(2)
    path, note = resolve_path(rel)
    if path is None:
        return False, note

    lines = read_lines(path)
    if not lines:
        return False, f"文件读不出来（空或无权限）：{rel}"

    shown = path.relative_to(REPO_ROOT).as_posix()
    parts = symbol_path.split("::")
    if len(parts) == 1:
        if symbol_defined(lines, parts[0]):
            return True, f"{shown}::{parts[0]}{note}"
        return _not_found(parts[0], rel=shown)

    # 类::方法 或 类::类::方法：逐级下沉
    current, rest = parts[0], parts[1:]
    start, end = _class_span(lines, current)
    if start < 0:
        return _not_found(current)
    for name in rest:
        # 方法/属性：在剩余范围内找定义
        found = False
        for line in lines[start:end]:
            if re.match(rf"^\s+(?:async\s+)?def\s+{re.escape(name)}\s*\(", line) or re.match(
                rf"^\s+{re.escape(name)}\s*(?::\s*[^=]+)?=", line
            ):
                found = True
                break
        if not found:
            return _not_found(name, inside=current, rel=shown)
        current = name
    return True, f"{shown}::{symbol_path}{note}"


def _not_found(symbol: str, *, inside: str | None = None, rel: str | None = None) -> tuple[bool, str]:
    """符号没找到时，顺手给出它真正在哪——这才有诊断价值。"""
    where = f"{rel} 里的 {inside}." if inside and rel else f"{rel} 里" if rel else "该文件里"
    hits = find_everywhere(symbol)
    if hits:
        first = hits[0]
        suggestion = f"{first[0]}:{first[1]}"
        extra = f"（共 {len(hits)} 处，其余：{', '.join(h[:1][0] for h in hits[1:4])}）" if len(hits) > 1 else ""
        return False, f"{where}找不到 {symbol}。它其实在 {suggestion}{extra}"
    return False, f"{where}找不到 {symbol}，全仓库也搜不到——名字可能记错了"


# --------------------------------------------------------------------------- #
# 三种模式
# --------------------------------------------------------------------------- #
def extract_anchors(text: str) -> list[str]:
    """从任意文本里抽出所有锚点（保持出现顺序、去重）。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for match in ANCHOR_RE.finditer(text):
        anchor = f"{match.group(1)}::{match.group(2)}"
        if anchor not in seen:
            seen.add(anchor)
            ordered.append(anchor)
    return ordered


def run_map(path: Path) -> int:
    """--map：校验一份 markdown 文档里的全部锚点。"""
    if not path.exists():
        print(f"文件不存在：{path}")
        return 2
    anchors = extract_anchors(path.read_text(encoding="utf-8", errors="replace"))
    if not anchors:
        print(f"{path} 里没找到任何锚点（格式：路径::符号）")
        return 2
    return _report(anchors, title=f"校验 {path}")


def run_answer(path: Path) -> int:
    """--answer：校验新人提交的答案，每行一个锚点，# 为注释。"""
    if not path.exists():
        print(f"文件不存在：{path}")
        return 2
    anchors: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 允许一行写多个（空格/逗号分隔），也允许夹带说明文字
        anchors.extend(extract_anchors(line))
        if not extract_anchors(line) and "::" in line:
            anchors.append(line)
    if not anchors:
        print("没解析到任何锚点。每行写一个，例如：src/flow_ops/leads.py::ALLOWED_TRANSITIONS")
        return 2
    return _report(anchors, title=f"校验答案 {path}")


def run_probe(symbol: str) -> int:
    """--probe：反查符号在哪个文件。"""
    hits = find_everywhere(symbol)
    if not hits:
        print(f"全仓库找不到 {symbol} 的定义")
        return 1
    print(f"{symbol} 出现在 {len(hits)} 处：")
    for rel, lineno in hits:
        print(f"  {rel}:{lineno}")
    return 0


def _report(anchors: list[str], *, title: str) -> int:
    passed = 0
    failed: list[tuple[str, str]] = []
    for anchor in anchors:
        ok, detail = check_anchor(anchor)
        if ok:
            passed += 1
            print(f"  ok    {detail}")
        else:
            failed.append((anchor, detail))
            print(f"  FAIL  {anchor}\n        {detail}")
    print(f"\n{title}：{passed}/{len(anchors)} 个锚点有效")
    if failed:
        print("失效锚点通常是两种原因：符号被改名/删除，或路径写错。改文档，不要绕过。")
        return 1
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验代码位置类答案/文档里的锚点是否真实存在",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--map", type=Path, help="校验一份 markdown 文档里的全部锚点")
    mode.add_argument("--answer", type=Path, help="校验新人提交的答案文件（每行一个锚点）")
    mode.add_argument("--probe", metavar="SYMBOL", help="反查符号定义在哪个文件")
    args = parser.parse_args(argv)

    if args.map:
        return run_map(args.map)
    if args.answer:
        return run_answer(args.answer)
    return run_probe(args.probe)


if __name__ == "__main__":
    sys.exit(main())
