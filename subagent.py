"""子 Agent 系统 — 三种内置类型 + 同一 Agent 类复用"""

from tools import TOOLS

# ─── 只读工具集 ──────────────────────────────────────────

READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search", "tool_search"}


def _get_read_only_tools() -> list[dict]:
    return [t for t in TOOLS if t["function"]["name"] in READ_ONLY_TOOLS]


def _get_full_tools() -> list[dict]:
    """除了 agent 工具自身，给全部工具（防止递归）"""
    return [t for t in TOOLS if t["function"]["name"] != "agent"]


# ─── Agent 类型定义 ──────────────────────────────────────

EXPLORE_PROMPT = """你是 Explore Agent — 快速只读代码搜索专家。

=== 严格约束 ===
- 只读模式：只能 read_file, list_files, grep_search
- 不能修改任何文件
- 不能执行 shell 命令

你的任务：高效搜索代码库，返回简洁的发现报告。
- 用多个工具调用同时搜索，提高效率
- 返回结果要精炼，只包含关键发现"""

PLAN_PROMPT = """你是 Plan Agent — 只读结构化方案设计专家。

=== 严格约束 ===
- 只读模式：只能 read_file, list_files, grep_search
- 不能修改任何文件
- 不能执行 shell 命令

返回结构化方案：
1. 当前状态摘要
2. 分步实现计划
3. 关键文件列表
4. 潜在风险"""

GENERAL_PROMPT = """你是 General 子 Agent，处理独立子任务。

完成分配的任务后返回简洁报告。你有完整工具集（除了不能再创建子 Agent）。
- 彻底完成任务，不要半途而废
- 返回简洁的结果摘要"""

# ─── 配置获取 ────────────────────────────────────────────

def get_sub_agent_config(agent_type: str) -> dict:
    """返回 {system_prompt, tools}"""
    if agent_type == "explore":
        return {"system_prompt": EXPLORE_PROMPT, "tools": _get_read_only_tools()}
    elif agent_type == "plan":
        return {"system_prompt": PLAN_PROMPT, "tools": _get_read_only_tools()}
    else:  # general
        return {"system_prompt": GENERAL_PROMPT, "tools": _get_full_tools()}
