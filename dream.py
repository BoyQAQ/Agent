"""Dreaming - idle-time reflection, extract patterns, improve skills"""

import json
import time
import threading

DREAM_PROMPT = """You are the Agent's "dream reflection" module. The user is away. Review recent work and self-improve.

## What you can do
- read_file: inspect existing SKILL.md files
- write_file: create new skills from discovered patterns
- edit_file: improve existing skills
- query_db: query sessions and skills tables for data
- list_files: see what skills exist
- web_fetch: check online docs if needed

## What to do
1. Query the sessions and skills tables for recent data
2. Look for:
   a. Repeated operations -> create a new Skill
   b. Low-success skills -> improve them
   c. Long-unused skills -> suggest deprecation
3. Output a brief summary of what you changed or decided

## Rules
- NO destructive operations (rm, force push, etc)
- Only modify .mini-agent/skills/ SKILL.md files
- If nothing worth improving, say "nothing to improve"
- Keep it short, this is background work
"""


class Dreamer:
    """Background dream thread with real tool execution"""

    def __init__(self, idle_seconds=60, check_interval=15):
        self._idle_seconds = idle_seconds
        self._check_interval = check_interval
        self._last_activity = time.time()
        self._thread = None
        self._running = False
        self._dream_result = ""
        self._dream_count = 0

    @property
    def result(self):
        r = self._dream_result
        self._dream_result = ""
        return r

    def touch(self):
        self._last_activity = time.time()

    @property
    def idle_time(self):
        return time.time() - self._last_activity

    @property
    def should_dream(self):
        return self.idle_time >= self._idle_seconds

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            time.sleep(self._check_interval)
            if self.should_dream and self._dream_count < 3:
                self._dream()

    def _dream(self):
        self._dream_count += 1
        try:
            from openai import OpenAI
            from agent import resolve_api_config
            from tools import TOOLS, execute_tool

            cfg = resolve_api_config()
            if not cfg["api_key"]:
                return
            client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])

            dream_tools = [t for t in TOOLS if t["function"]["name"] in {
                "query_db", "read_file", "list_files", "grep_search",
                "write_file", "edit_file", "web_fetch",
            }]

            messages = [
                {"role": "system", "content": DREAM_PROMPT},
                {"role": "user", "content": f"Time: {time.strftime('%Y-%m-%d %H:%M')}\n\nQuery the database, review skills and sessions. Improve if needed."},
            ]

            summary = ""
            for _ in range(5):
                resp = client.chat.completions.create(
                    model=cfg["model"],
                    messages=messages,
                    tools=dream_tools,
                )
                am = resp.choices[0].message
                tcs = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in (am.tool_calls or [])
                ] if am.tool_calls else None
                messages.append({"role": "assistant", "content": am.content, "tool_calls": tcs})

                if am.content:
                    summary = am.content
                if not am.tool_calls:
                    break

                for tc in am.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:
                        args = {}
                    result = execute_tool(tc.function.name, args)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)[:3000]})

            if summary and "nothing to improve" not in summary.lower():
                self._dream_result = summary
                self._last_activity = time.time()
        except Exception:
            pass


_dreamer = None


def get_dreamer(idle_seconds=60):
    global _dreamer
    if _dreamer is None:
        _dreamer = Dreamer(idle_seconds=idle_seconds)
    return _dreamer
