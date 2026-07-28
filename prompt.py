"""System Prompt 动态注入 — CLAUDE.md 层级发现 + Git 上下文"""

import os
import locale
import re
import subprocess
from datetime import date
from pathlib import Path

# @include 常量
INCLUDE_PATTERN = re.compile(r"^@(\./[^\s]+|~/[^\s]+|/[^\s]+)$")
MAX_INCLUDE_DEPTH = 5


def _substitute_vars(content: str) -> str:
    """替换 CLAUDE.md 中的变量占位符"""
    from datetime import date
    import platform
    return (content
        .replace("{{cwd}}", str(Path.cwd()))
        .replace("{{date}}", date.today().isoformat())
        .replace("{{platform}}", platform.system())
        .replace("{{home}}", str(Path.home()))
    )


def _resolve_includes(content: str, base_dir: Path,
                      visited: set | None = None, depth: int = 0) -> str:
    """
    解析 CLAUDE.md 里的 @include 指令。
    支持三种路径: @./relative  @~/home  @/absolute
    防护: 循环检测(visited) + 最大深度(5)
    """
    if visited is None:
        visited = set()
    if depth >= MAX_INCLUDE_DEPTH:
        return content

    result: list[str] = []
    for line in content.split("\n"):
        m = INCLUDE_PATTERN.match(line.strip())
        if not m:
            result.append(line)
            continue

        raw = m.group(1)

        # 解析路径
        if raw.startswith("~/"):
            target = Path.home() / raw[2:]
        elif raw.startswith("/"):
            target = Path(raw)
        else:  # ./
            target = (base_dir / raw[2:]).resolve()

        # 循环检测
        key = str(target)
        if key in visited:
            result.append(f"<!-- circular: {raw} -->")
            continue

        if not target.is_file():
            result.append(f"<!-- not found: {raw} -->")
            continue

        try:
            visited.add(key)
            included = target.read_text(encoding="utf-8")
            # 递归解析被引用文件里的 @include
            resolved = _resolve_includes(
                included, target.parent, visited, depth + 1
            )
            result.append(resolved)
        except UnicodeDecodeError:
            try:
                included = target.read_text(encoding="gbk")
                result.append(_resolve_includes(
                    included, target.parent, visited, depth + 1
                ))
            except Exception:
                result.append(f"<!-- encoding error: {raw} -->")
        except Exception:
            result.append(f"<!-- error reading: {raw} -->")

    return "\n".join(result)


def _load_claude_md() -> str:
    """从当前目录往上找 CLAUDE.md，越靠近的越后拼接 → 近因效应"""
    parts = []
    d = Path.cwd().resolve()
    while True:
        f = d / "CLAUDE.md"
        if f.is_file():
            try:
                raw = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    raw = f.read_text(encoding="gbk")
                except Exception:
                    raw = ""
            except Exception:
                raw = ""

            if raw:
                raw = _resolve_includes(raw, d)
                raw = _substitute_vars(raw)
                parts.insert(0, raw)

        parent = d.parent
        if parent == d:
            break
        d = parent

    if not parts:
        return ""
    return "\n\n" + "\n\n---\n\n".join(parts)


def _get_git_context() -> str:
    """获取当前 git 仓库上下文（branch + 最近 5 commits + status）"""
    enc = locale.getpreferredencoding()
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=3
        ).strip().decode(enc, errors="replace")
        log = subprocess.check_output(
            ["git", "log", "--oneline", "-5"],
            timeout=3
        ).strip().decode(enc, errors="replace")
        status = subprocess.check_output(
            ["git", "status", "--short"],
            timeout=3
        ).strip().decode(enc, errors="replace")
        parts = [f"Git branch: {branch}"]
        if log:
            parts.append(f"Recent commits:\n{log}")
        if status:
            parts.append(f"Git status:\n{status}")
        return "\n".join(parts)
    except Exception:
        return ""


def build_system_prompt() -> str:
    """拼装最终 system prompt —— 模板 + 动态变量"""
    cwd = Path.cwd()
    claude_md = _load_claude_md()
    git_ctx = _get_git_context()
    from memory import build_memory_prompt_section
    from skills import build_skill_descriptions
    memory_section = build_memory_prompt_section()
    skills_section = build_skill_descriptions()

    return f"""你是一个编程助手。你可以读取文件、写入文件、执行命令。
优先使用工具而不是凭记忆猜测。

# 环境
工作目录: {cwd}
日期: {date.today()}
操作系统: {os.name}

{git_ctx if git_ctx else ""}
{claude_md if claude_md else ""}

# 规则
- 先读代码再修改，不要凭记忆猜测文件内容
- 不要过度工程，只做用户要求的事
- 危险操作（rm、git push --force、sudo）需要格外小心

{memory_section}"""
