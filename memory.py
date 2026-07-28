"""记忆系统 — 4 种类型、文件存储、语义召回、异步预取"""

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ─── 路径管理 ────────────────────────────────────────────

def _project_hash() -> str:
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]


def get_memory_dir() -> Path:
    d = Path.home() / ".mini-agent" / "projects" / _project_hash() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── 常量 ────────────────────────────────────────────────

VALID_TYPES = {"user", "feedback", "project", "reference"}
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000
MAX_MEMORY_FILES = 200
MAX_MEMORY_BYTES_PER_FILE = 4096
MAX_SESSION_MEMORY_BYTES = 60 * 1024

SideQueryFn = Callable[[str, str], Any]  # async (system, user_msg) -> str


# ─── 数据结构 ────────────────────────────────────────────

class MemoryHeader:
    __slots__ = ("filename", "file_path", "mtime_ms", "description", "type")

    def __init__(self, filename: str, file_path: str, mtime_ms: float,
                 description: str | None, type: str | None):
        self.filename = filename
        self.file_path = file_path
        self.mtime_ms = mtime_ms
        self.description = description
        self.type = type


class RelevantMemory:
    __slots__ = ("path", "content", "mtime_ms", "header")

    def __init__(self, path: str, content: str, mtime_ms: float, header: str):
        self.path = path
        self.content = content
        self.mtime_ms = mtime_ms
        self.header = header


# ─── 记忆 CRUD ───────────────────────────────────────────

def list_memories() -> list[dict]:
    """列出所有记忆（返回 {name, description, type, filename, content}）"""
    d = get_memory_dir()
    entries = []
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            raw = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, Exception):
            try:
                raw = f.read_text(encoding="gbk")
            except Exception:
                continue
        meta, body = _parse_frontmatter(raw)
        if not meta.get("name"):
            continue
        t = meta.get("type", "project")
        if t not in VALID_TYPES:
            t = "project"
        entries.append({
            "name": meta["name"],
            "description": meta.get("description", ""),
            "type": t,
            "filename": f.name,
            "content": body,
            "mtime": f.stat().st_mtime,
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """简易 YAML frontmatter 解析"""
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, raw
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end == -1:
        return {}, raw
    meta = {}
    for line in lines[1:end]:
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    body = "\n".join(lines[end + 1:]).strip()
    return meta, body


# ─── MEMORY.md 索引 ──────────────────────────────────────

def _build_index() -> str:
    memories = list_memories()
    lines = ["# Memory Index", ""]
    for m in memories:
        lines.append(
            f"- **[{m['name']}]({m['filename']})** "
            f"({m['type']}) — {m['description']}"
        )
    return "\n".join(lines)


def load_memory_index() -> str:
    """加载索引文件，带截断保护"""
    idx_path = get_memory_dir() / "MEMORY.md"
    if not idx_path.exists():
        return _build_index()  # 首次生成
    content = idx_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[...truncated...]"
    if len(content.encode()) > MAX_INDEX_BYTES:
        content = content[:MAX_INDEX_BYTES] + "\n\n[...truncated...]"
    return content


# ─── Memory Header 快速扫描 ──────────────────────────────

def scan_memory_headers() -> list[MemoryHeader]:
    """只读每个文件的前 30 行（frontmatter），不读完整内容"""
    d = get_memory_dir()
    headers = []
    for f in d.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            stat = f.stat()
            try:
                raw = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raw = f.read_text(encoding="gbk")
            first30 = "\n".join(raw.split("\n")[:30])
            meta, _ = _parse_frontmatter(first30)
            t = meta.get("type")
            headers.append(MemoryHeader(
                filename=f.name,
                file_path=str(f),
                mtime_ms=stat.st_mtime * 1000,
                description=meta.get("description"),
                type=t if t in VALID_TYPES else None,
            ))
        except Exception:
            pass
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers[:MAX_MEMORY_FILES]


# ─── 新鲜度警告 ──────────────────────────────────────────

def memory_age(mtime_ms: float) -> str:
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    return f"{days} 天前"


def memory_freshness_warning(mtime_ms: float) -> str:
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days <= 1:
        return ""
    return (
        f"⚠ 这条记忆是 {days} 天前保存的。记忆是时间快照而非实时状态，"
        f"其中的信息可能已过时，请以当前代码为准。"
    )


# ─── 语义召回（sideQuery）─────────────────────────────────

SELECT_MEMORIES_PROMPT = """你正在为一个 AI 编程助手选择相关记忆。
你会收到用户的查询和可用的记忆文件列表（文件名 + 描述）。

返回一个 JSON 对象，其中 "selected_memories" 是一个文件名数组（最多 5 个）。
只选择根据名称和描述确实会用到的记忆。
- 如果不确定某条记忆是否有用，就不要选。
- 如果没有明显有用的记忆，返回空数组。"""


async def select_relevant_memories(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
) -> list[RelevantMemory]:
    """调用模型做语义匹配，选择相关记忆"""
    headers = scan_memory_headers()
    if not headers:
        return []

    candidates = [h for h in headers if h.file_path not in already_surfaced]
    if not candidates:
        return []

    # 构建清单
    manifest_lines = []
    for h in candidates:
        tag = f"[{h.type}] " if h.type else ""
        ts = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc).isoformat()
        desc = h.description or ""
        manifest_lines.append(f"- {tag}{h.filename} ({ts}): {desc}")
    manifest = "\n".join(manifest_lines)

    try:
        text = await side_query(
            SELECT_MEMORIES_PROMPT,
            f"Query: {query}\n\nAvailable memories:\n{manifest}",
        )

        # 从响应中提取 JSON
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return []

        parsed = json.loads(match.group(0))
        selected_names = set(parsed.get("selected_memories", []))

        selected = [h for h in candidates if h.filename in selected_names][:5]
        result = []
        for h in selected:
            content = Path(h.file_path).read_text(encoding="utf-8")
            # 4KB 截断
            if len(content.encode()) > MAX_MEMORY_BYTES_PER_FILE:
                content = content[:MAX_MEMORY_BYTES_PER_FILE] + "\n\n[...truncated...]"

            freshness = memory_freshness_warning(h.mtime_ms)
            header = (
                f"{freshness}\n\nMemory: {h.file_path}:"
                if freshness
                else f"Memory (保存于 {memory_age(h.mtime_ms)}): {h.file_path}:"
            )
            result.append(RelevantMemory(
                path=h.file_path, content=content,
                mtime_ms=h.mtime_ms, header=header,
            ))
        return result
    except Exception:
        return []


# ─── 异步预取 ────────────────────────────────────────────

class MemoryPrefetch:
    def __init__(self, task: asyncio.Task):
        self.task = task
        self.consumed = False

    @property
    def settled(self) -> bool:
        return self.task.done()


def start_memory_prefetch(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
    session_memory_bytes: int,
) -> MemoryPrefetch | None:
    """启动异步记忆预取，返回句柄用于后续轮询"""
    # 门控 1: 单词查询跳过
    if not re.search(r"\s", query.strip()):
        return None
    # 门控 2: 会话预算已满
    if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
        return None
    # 门控 3: 没有记忆文件
    d = get_memory_dir()
    has_memories = any(
        f.suffix == ".md" and f.name != "MEMORY.md" for f in d.iterdir()
    )
    if not has_memories:
        return None

    task = asyncio.create_task(
        select_relevant_memories(query, side_query, already_surfaced)
    )
    return MemoryPrefetch(task)


def format_memories_for_injection(memories: list[RelevantMemory]) -> str:
    """将召回的记忆格式化为注入文本"""
    parts = []
    for m in memories:
        parts.append(
            f"<system-reminder>\n{m.header}\n\n{m.content}\n</system-reminder>"
        )
    return "\n\n".join(parts)


# ─── System Prompt 注入 ─────────────────────────────────

def build_memory_prompt_section() -> str:
    """生成注入到 system prompt 的记忆系统说明"""
    index = load_memory_index()
    memory_dir = str(get_memory_dir())

    return f"""# Memory System

你有一个持久化的文件记忆系统，位于 `{memory_dir}`。

## 记忆类型
- **user**: 用户角色、偏好、知识水平
- **feedback**: 用户的纠正和肯定（含 Why + How to apply）
- **project**: 项目进展、目标、截止日期、决策（用绝对日期）
- **reference**: 外部资源指针（URL、工具、面板地址）

## 如何保存记忆
用 write_file 创建记忆文件，YAML frontmatter 格式：

```markdown
---
name: 记忆名称
description: 一行描述
type: user|feedback|project|reference
---
记忆正文内容。
```

保存到: `{memory_dir}/`
文件名格式: `{{type}}_{{slugified_name}}.md`

## 什么不应该保存
- 代码模式或架构（读代码即可获得）
- Git 历史（用 git log）
- CLAUDE.md 已有的内容
- 临时任务细节

{"## 当前记忆索引" + chr(10) + index if index else "(暂无记忆)"}"""
