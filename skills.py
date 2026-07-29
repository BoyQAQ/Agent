"""技能系统 — SKILL.md 模板 + 双重调用路径 + inline/fork 执行 + 自进化追踪"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from memory import _parse_frontmatter


# ─── 数据结构 ────────────────────────────────────────────

@dataclass
class SkillDefinition:
    name: str
    description: str = ""
    when_to_use: str | None = None
    allowed_tools: list[str] | None = None
    user_invocable: bool = True
    context: str = "inline"  # "inline" or "fork"
    prompt_template: str = ""
    source: str = "project"  # "project" or "user"
    skill_dir: str = ""


# ─── 技能发现（带热加载）─────────────────────────────────

_cached_skills: list[SkillDefinition] | None = None
_cache_mtime: float = 0.0  # 上次扫描时目录的最新修改时间


def _get_dirs_mtime() -> float:
    """获取所有技能目录的最新 mtime，用于检测变化"""
    max_mtime = 0.0
    for base in (Path.home() / ".mini-agent" / "skills",
                 Path.cwd() / ".mini-agent" / "skills"):
        if not base.is_dir():
            continue
        try:
            st = base.stat().st_mtime
            if st > max_mtime:
                max_mtime = st
            # 也检查子目录
            for entry in base.iterdir():
                if entry.is_dir():
                    sf = entry / "SKILL.md"
                    if sf.is_file():
                        st2 = sf.stat().st_mtime
                        if st2 > max_mtime:
                            max_mtime = st2
        except Exception:
            continue
    return max_mtime


def discover_skills() -> list[SkillDefinition]:
    """扫描 ~/.mini-agent/skills/ 和 ./.mini-agent/skills/，project 覆盖 user。
    自动检测文件变化，无需手动刷新。"""
    global _cached_skills, _cache_mtime

    # 检查是否有变化
    current_mtime = _get_dirs_mtime()
    if _cached_skills is not None and current_mtime <= _cache_mtime:
        return _cached_skills

    skills: dict[str, SkillDefinition] = {}

    # 用户级（低优先级）
    _load_from_dir(Path.home() / ".mini-agent" / "skills", "user", skills)
    # 项目级（高优先级，同名覆盖）
    _load_from_dir(Path.cwd() / ".mini-agent" / "skills", "project", skills)

    _cached_skills = list(skills.values())
    _cache_mtime = current_mtime
    return _cached_skills


def _load_from_dir(base: Path, source: str, skills: dict[str, SkillDefinition]):
    if not base.is_dir():
        return
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        sf = entry / "SKILL.md"
        if not sf.is_file():
            continue
        try:
            raw = sf.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                raw = sf.read_text(encoding="gbk")
            except Exception:
                continue
        except Exception:
            continue

        meta, body = _parse_frontmatter(raw)
        name = meta.get("name") or entry.name

        # 解析 allowed-tools
        allowed = None
        if "allowed-tools" in meta:
            raw_tools = meta["allowed-tools"]
            if raw_tools.startswith("["):
                try:
                    allowed = json.loads(raw_tools)
                except Exception:
                    allowed = [s.strip() for s in raw_tools.strip("[]").split(",")]
            else:
                allowed = [s.strip() for s in raw_tools.split(",")]

        skills[name] = SkillDefinition(
            name=name,
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use") or meta.get("when-to-use"),
            allowed_tools=allowed,
            user_invocable=meta.get("user-invocable", "true") != "false",
            context="fork" if meta.get("context") == "fork" else "inline",
            prompt_template=body,
            source=source,
            skill_dir=str(entry),
        )


# ─── Prompt 解析 ─────────────────────────────────────────

def get_skill_by_name(name: str) -> SkillDefinition | None:
    for s in discover_skills():
        if s.name == name:
            return s
    return None


def resolve_skill_prompt(skill: SkillDefinition, args: str) -> str:
    """替换 $ARGUMENTS 和 ${CLAUDE_SKILL_DIR}"""
    prompt = skill.prompt_template
    prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", args, prompt)
    prompt = prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)
    return prompt


def execute_skill(skill_name: str, args: str) -> dict | None:
    """返回 {"prompt": ..., "allowed_tools": ..., "context": ...} 或 None"""
    skill = get_skill_by_name(skill_name)
    if not skill:
        return None
    return {
        "prompt": resolve_skill_prompt(skill, args),
        "allowed_tools": skill.allowed_tools,
        "context": skill.context,
        "skill_dir": skill.skill_dir,
    }


# ─── System Prompt 注入 ─────────────────────────────────

def build_skill_descriptions() -> str:
    """生成注入到 system prompt 的技能列表"""
    skills = discover_skills()
    if not skills:
        return ""

    lines = ["# Available Skills", ""]
    invocable = [s for s in skills if s.user_invocable]
    auto_only = [s for s in skills if not s.user_invocable]

    if invocable:
        lines.append("用户可调用技能（使用 /<name> 触发）：")
        for s in invocable:
            lines.append(f"- **/{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  触发条件: {s.when_to_use}")
        lines.append("")

    if auto_only:
        lines.append("自动触发技能（模型判断需要时调用 skill 工具）：")
        for s in auto_only:
            lines.append(f"- **{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  触发条件: {s.when_to_use}")
        lines.append("")

    lines.append("使用 `skill` 工具按名称调用技能，可传入可选参数。")
    return "\n".join(lines)


def reset_skill_cache():
    global _cached_skills, _cache_mtime
    _cached_skills = None
    _cache_mtime = 0.0


# ─── 使用追踪（自进化数据）─────────────────────────────────

def _usage_path() -> Path:
    d = Path.cwd() / ".mini-agent" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d / ".usage.json"


def _load_usage() -> dict:
    p = _usage_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_usage(data: dict):
    _usage_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def record_skill_usage(skill_name: str, success: bool):
    """记录一次技能使用（模型调用 skill 工具时自动记录）"""
    data = _load_usage()
    entry = data.get(skill_name, {
        "total": 0,
        "success": 0,
        "last_used": 0,
        "created": time.time(),
    })
    entry["total"] += 1
    if success:
        entry["success"] += 1
    entry["last_used"] = time.time()
    data[skill_name] = entry
    _save_usage(data)


def get_skill_stats(skill_name: str) -> dict | None:
    """查询单个技能的使用统计"""
    return _load_usage().get(skill_name)


def get_stale_skills(days: int = 30, min_uses: int = 3) -> list[str]:
    """检测可能废弃的技能：N 天未使用但曾被用过 M 次以上"""
    data = _load_usage()
    cutoff = time.time() - days * 86400
    stale = []
    for name, entry in data.items():
        if entry["last_used"] < cutoff and entry["total"] >= min_uses:
            stale.append(name)
    return stale


def get_skill_suggestions() -> str:
    """生成技能改进建议，注入到 system prompt"""
    stale = get_stale_skills()
    if not stale:
        return ""
    lines = ["\n⚠ 以下技能较久未使用，考虑检查是否需要更新或废弃："]
    for name in stale:
        stats = get_skill_stats(name)
        if stats:
            rate = stats["success"] / max(stats["total"], 1) * 100
            lines.append(f"- {name}: 使用 {stats['total']} 次, 成功率 {rate:.0f}%")
    return "\n".join(lines)
