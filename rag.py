"""RAG 轻量版 — TF-IDF 代码检索，零外部依赖"""

import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path

# ─── 配置 ────────────────────────────────────────────────

MAX_FILE_KB = 200          # 跳过过大的文件
MAX_CHUNKS = 200           # 最多索引块数
CHUNK_LINES = 40           # 无函数时，按行分块
TOP_K = 5                  # 默认返回 Top-K 结果
CACHE_TTL = 300            # 缓存有效期（秒）


# ─── 代码分块 ────────────────────────────────────────────

_FUNC_RE = re.compile(
    r'^\s*(?:(?:async\s+)?def\s+\w+|class\s+\w+)',
    re.MULTILINE,
)

_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp",
               ".h", ".rb", ".sh", ".bash", ".yaml", ".yml", ".toml",
               ".json", ".md", ".sql", ".html", ".css", ".vue", ".svelte"}


def _tokenize(text: str) -> list[str]:
    """分词：拆分驼峰、下划线和运算符"""
    # 先按 snake_case 和 camelCase 拆分
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)
    # 提取字母数字序列（至少2个字符）
    tokens = re.findall(r'[a-zA-Z_]\w+', text.lower())
    return [t for t in tokens if len(t) >= 2]


def _chunk_file(file_path: Path) -> list[dict]:
    """将文件分成代码块，优先按函数/类边界"""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = content.split("\n")
    if len(lines) < 3:
        return []

    # 找函数/类定义行
    func_lines = [0]
    for i, line in enumerate(lines):
        if _FUNC_RE.match(line):
            func_lines.append(i)

    chunks = []
    for j in range(len(func_lines)):
        start = func_lines[j]
        end = func_lines[j + 1] if j + 1 < len(func_lines) else len(lines)
        chunk_lines = lines[start:end]
        if len(chunk_lines) < 2:
            continue
        chunk_text = "\n".join(chunk_lines)
        chunks.append({
            "file": str(file_path),
            "start": start + 1,
            "end": end,
            "text": chunk_text[:4000],
        })

    # 如果没有函数分界，按固定行数分块
    if not chunks:
        for i in range(0, len(lines), CHUNK_LINES):
            chunk_lines = lines[i:i + CHUNK_LINES]
            if len(chunk_lines) < 2:
                continue
            chunks.append({
                "file": str(file_path),
                "start": i + 1,
                "end": min(i + CHUNK_LINES, len(lines)),
                "text": "\n".join(chunk_lines)[:4000],
            })

    return chunks[:20]  # 每文件最多20块


# ─── TF-IDF 索引 ─────────────────────────────────────────

class CodeIndex:
    def __init__(self):
        self.chunks: list[dict] = []          # 所有代码块
        self.doc_tokens: list[list[str]] = []  # 每块的 tokens
        self.idf: dict[str, float] = {}        # IDF 值
        self._dirty = False

    def build(self, root_dir: str | None = None) -> int:
        """扫描项目，构建索引。返回索引块数。"""
        root = Path(root_dir) if root_dir else Path.cwd()
        self.chunks = []
        self.doc_tokens = []

        for f in root.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix not in _EXTENSIONS:
                continue
            if f.stat().st_size > MAX_FILE_KB * 1024:
                continue
            # 跳过常见忽略目录
            parts = set(f.parts)
            if parts & {".git", "__pycache__", "node_modules", ".venv",
                        "venv", "dist", "build", ".next", ".cache", "target"}:
                continue
            if len(self.chunks) >= MAX_CHUNKS:
                break

            for chunk in _chunk_file(f):
                if len(self.chunks) >= MAX_CHUNKS:
                    break
                tokens = _tokenize(chunk["text"])
                if len(tokens) < 3:
                    continue
                self.chunks.append(chunk)
                self.doc_tokens.append(tokens)

        self._compute_idf()
        self._dirty = False
        return len(self.chunks)

    def _compute_idf(self):
        """计算 IDF"""
        N = len(self.doc_tokens)
        if N == 0:
            return
        df = defaultdict(int)
        for tokens in self.doc_tokens:
            for t in set(tokens):
                df[t] += 1
        self.idf = {t: math.log((N + 1) / (df[t] + 1)) + 1 for t in df}

    def search(self, query: str, top_k: int = TOP_K) -> list[dict]:
        """搜索 Top-K 相关代码块"""
        if not self.chunks:
            return []

        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        # 计算 query TF
        q_tf = defaultdict(float)
        for t in q_tokens:
            q_tf[t] += 1.0
        # 归一化
        q_norm = math.sqrt(sum(v * v for v in q_tf.values()))
        if q_norm > 0:
            for t in q_tf:
                q_tf[t] /= q_norm

        scores = []
        for i, doc_toks in enumerate(self.doc_tokens):
            # doc TF
            d_tf = defaultdict(float)
            for t in doc_toks:
                d_tf[t] += 1.0
            d_norm = math.sqrt(sum(v * v for v in d_tf.values()))
            if d_norm == 0:
                continue

            # cosine similarity
            dot = 0.0
            for t, qv in q_tf.items():
                dot += qv * (d_tf.get(t, 0) / d_norm) * self.idf.get(t, 0)
            if dot > 0.01:
                scores.append((i, dot))

        scores.sort(key=lambda x: -x[1])
        results = []
        for idx, score in scores[:top_k]:
            c = dict(self.chunks[idx])
            c["score"] = round(score, 3)
            results.append(c)

        return results


# ─── 全局单例 ────────────────────────────────────────────

_index: CodeIndex | None = None
_cache_time: float = 0.0
_index_dir: str = ""


def _get_cache_path() -> Path:
    import hashlib
    d = Path.cwd()
    h = hashlib.sha256(str(d).encode()).hexdigest()[:12]
    return Path.home() / ".mini-agent" / "rag_cache" / f"{h}.json"


def _load_cache() -> bool:
    global _index, _cache_time, _index_dir
    p = _get_cache_path()
    if not p.is_file():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        _index = CodeIndex()
        _index.chunks = data["chunks"]
        _index.doc_tokens = [[str(t) for t in ts] for ts in data["doc_tokens"]]
        _index.idf = data["idf"]
        _cache_time = time.time()
        _index_dir = str(Path.cwd())
        return True
    except Exception:
        return False


def _save_cache():
    if _index is None:
        return
    p = _get_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "chunks": _index.chunks,
        "doc_tokens": _index.doc_tokens,
        "idf": _index.idf,
    }
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def get_index(force_rebuild: bool = False) -> CodeIndex:
    global _index, _cache_time, _index_dir
    cwd = str(Path.cwd())

    # 目录变了，重建
    if _index_dir != cwd:
        _index = None

    if _index is not None and not force_rebuild:
        if time.time() - _cache_time < CACHE_TTL:
            return _index

    # 尝试加载缓存
    if not force_rebuild and _load_cache() and _index_dir == cwd:
        return _index

    # 重建索引
    _index = CodeIndex()
    n = _index.build()
    _cache_time = time.time()
    _index_dir = cwd
    if n > 0:
        _save_cache()
    return _index


def search(query: str, top_k: int = TOP_K) -> list[dict]:
    """便捷搜索接口"""
    idx = get_index()
    return idx.search(query, top_k)


def rag_context(query: str, max_chars: int = 3000) -> str:
    """
    根据用户查询检索相关代码，返回格式化的上下文文本。
    直接注入 system prompt。
    """
    results = search(query, top_k=TOP_K)
    if not results:
        return ""

    lines = ["\n# 相关代码（RAG 检索）\n"]
    total = 0
    for r in results:
        block = f"`{r['file']}` L{r['start']}-{r['end']} (score:{r['score']}):\n```\n{r['text']}\n```\n"
        if total + len(block) > max_chars:
            break
        lines.append(block)
        total += len(block)

    return "\n".join(lines)
