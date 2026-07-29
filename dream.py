"""Dreaming - idle-time reflection, extract patterns, improve skills"""

import time
import threading

DREAM_PROMPT = """You are the Agent's "dream reflection" module. The user is away. Review recent work and self-improve.

## What you can do
- read_file: inspect existing SKILL.md files
- write_file: create new skills from discovered patterns
- edit_file: improve existing skills
- query_db: query sessions and skills tables for data

## What to do
1. Query the sessions table for recent conversations
2. Check skill usage stats from the skills table
3. Look for:
   a. Repeated operations -> create a new Skill
   b. Low-success skills -> improve them
   c. Long-unused skills -> suggest deprecation
4. Output a brief summary

## Rules
- NO destructive operations (rm, force push, etc)
- Only modify .mini-agent/skills/ SKILL.md files
- If nothing worth improving, say "nothing to improve"
- Keep it short, this is background work
"""


class Dreamer:
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

            cfg = resolve_api_config()
            if not cfg["api_key"]:
                return
            client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])

            session_info = ""
            try:
                from db import get_connection
                import hashlib
                from pathlib import Path
                ph = hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]
                conn = get_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT name, saved_at FROM sessions WHERE project_hash=%s ORDER BY updated_at DESC LIMIT 3",
                            (ph,),
                        )
                        for r in cur.fetchall():
                            session_info += f"  {r['name']} ({r['saved_at']})\n"
                finally:
                    conn.close()
            except Exception:
                pass

            skills_info = ""
            try:
                from skills import discover_skills, get_skill_stats
                for s in discover_skills():
                    st = get_skill_stats(s.name)
                    if st:
                        skills_info += f"  {s.name}: {st['total']} uses, {st['success']}/{st['total']} ok\n"
                    else:
                        skills_info += f"  {s.name}: never used\n"
            except Exception:
                pass

            msg = f"Time: {time.strftime('%Y-%m-%d %H:%M')}\n\nRecent sessions:\n{session_info or '(none)'}\n\nSkills:\n{skills_info or '(none)'}\n\nReview and decide if anything needs improvement."

            resp = client.chat.completions.create(
                model=cfg["model"],
                messages=[
                    {"role": "system", "content": DREAM_PROMPT},
                    {"role": "user", "content": msg},
                ],
            )
            content = resp.choices[0].message.content or ""
            if content and "nothing to improve" not in content.lower():
                self._dream_result = content
                self._last_activity = time.time()
        except Exception:
            pass


_dreamer = None


def get_dreamer(idle_seconds=60):
    global _dreamer
    if _dreamer is None:
        _dreamer = Dreamer(idle_seconds=idle_seconds)
    return _dreamer
