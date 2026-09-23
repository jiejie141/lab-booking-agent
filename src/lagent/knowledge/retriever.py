"""规范检索：手写 BM25 + 可选向量路径 + RRF 融合。

**这一版刻意修掉了上个项目踩过的坑。** 上个项目的 RRF 融合用 ``chunk.id``
做两路结果的合并键，而 BM25 那一路的 id 是 ``source#N``、向量那一路是
``instance_id#N`` —— 两边永远不相等，于是「两路互相印证」这个机制从头到尾
没有生效过，同一篇文档还会在结果里出现两次。当时测试全绿，因为那个断言只
检查了「分数高于某阈值」，而单路 rank0 的分数恰好也能过。

这里的合并键改成**内容指纹** ``(source, heading, text)``：不管哪一路召回的，
同一篇文档一定得到同一个键。并且专门加了一条断言「融合结果里不存在重复
正文」+「两路都命中的文档分数严格高于单路 rank0 的 1/(k+1)」，
让这个 bug 再也藏不住。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings
from ..schemas import DocHit

DEFAULT_CORPUS = Path(__file__).parent / "safety_rules.md"

_ASCII = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")

# 中文没有空格分词，用字符二元组做词元；英文与数字按词切。
# 二元组对中文短查询的召回明显好于单字，成本又远低于引入分词库。
def tokenize(text: str) -> list[str]:
    low = text.lower()
    tokens: list[str] = _ASCII.findall(low)
    for run in _CJK.findall(low):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass(frozen=True)
class Chunk:
    source: str
    heading: str
    text: str

    @property
    def fingerprint(self) -> str:
        """内容指纹 —— 融合与去重的唯一依据。"""
        raw = f"{self.source}\x00{self.heading}\x00{self.text}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def body(self) -> str:
        return f"{self.heading}：{self.text}"


def load_corpus(path: Path | None = None) -> list[Chunk]:
    """按 ``## `` 标题切块。规范文档天然以小节为单位，不需要滑窗。"""
    target = path or DEFAULT_CORPUS
    lines = target.read_text(encoding="utf-8").splitlines()
    source = lines[0].lstrip("# ").strip() if lines else target.stem

    chunks: list[Chunk] = []
    heading: str | None = None
    buf: list[str] = []
    for line in lines[1:]:
        if line.startswith("## "):
            if heading:
                body = "\n".join(buf).strip()
                if body:
                    chunks.append(Chunk(source=source, heading=heading, text=body))
            heading = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if heading:
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(source=source, heading=heading, text=body))
    return chunks


# --------------------------------------------------------------------------
# 检索器接口
# --------------------------------------------------------------------------
class Retriever:
    name = "base"

    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]:
        raise NotImplementedError

    def stats(self) -> dict:
        return {"name": self.name}


class BM25Index(Retriever):
    """手写 BM25（k1=1.5, b=0.75），不引入 rank_bm25。"""

    name = "bm25"

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.docs = [tokenize(c.body()) for c in chunks]
        self.n = len(self.docs)
        self.avgdl = (sum(len(d) for d in self.docs) / self.n) if self.n else 0.0
        self.tf = [Counter(d) for d in self.docs]

        df: Counter[str] = Counter()
        for doc in self.docs:
            df.update(set(doc))
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def score(self, query: str) -> list[tuple[Chunk, float]]:
        terms = tokenize(query)
        scored: list[tuple[Chunk, float]] = []
        if not terms or not self.n:
            return scored
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i]) or 1
            total = 0.0
            for term in terms:
                freq = tf.get(term)
                if not freq:
                    continue
                idf = self.idf.get(term, 0.0)
                denom = freq + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                total += idf * (freq * (self.k1 + 1)) / denom
            if total > 0:
                scored.append((self.chunks[i], total))
        scored.sort(key=lambda pair: (-pair[1], pair[0].heading))
        return scored

    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]:
        return self.score(query)[:k]

    def stats(self) -> dict:
        return {"name": self.name, "chunks": self.n, "vocab": len(self.idf)}


class ChromaVectorIndex(Retriever):
    """可选的向量路径。chromadb 未安装时 ``available()`` 为 False。

    刻意做成可选：核心流程（规则校验、并发下单、协商）不依赖向量库，
    没装 chromadb 时 hybrid 会降级回 BM25，并如实记录降级原因。
    """

    name = "vector"

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._collection = None
        self.last_error: str | None = None
        try:
            import chromadb

            client = chromadb.EphemeralClient()
            self._collection = client.create_collection("lab_safety_rules")
            self._collection.add(
                ids=[c.fingerprint for c in chunks],
                documents=[c.body() for c in chunks],
            )
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - 取决于环境是否装了 chromadb
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._collection = None

    def available(self) -> bool:
        return self._collection is not None

    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]:
        if self._collection is None:
            return []
        by_fp = {c.fingerprint: c for c in self.chunks}
        try:
            raw = self._collection.query(query_texts=[query], n_results=k)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            # 保住 last_error，而不是静默返回空 —— 空结果与「检索坏了」是两件事
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        out: list[tuple[Chunk, float]] = []
        ids = (raw.get("ids") or [[]])[0]
        dists = (raw.get("distances") or [[]])[0]
        for idx, doc_id in enumerate(ids):
            chunk = by_fp.get(doc_id)
            if chunk is None:
                continue
            dist = dists[idx] if idx < len(dists) else 1.0
            out.append((chunk, 1.0 / (1.0 + float(dist))))
        return out

    def stats(self) -> dict:
        return {
            "name": self.name,
            "available": self.available(),
            "last_error": self.last_error,
        }


@dataclass
class FusionTrace:
    """融合过程的观测记录，评测与调试都读它。"""

    paths: list[str] = field(default_factory=list)
    counter: dict[str, int] = field(default_factory=dict)


class HybridRetriever(Retriever):
    """RRF 融合：只吃排名，不吃分数。

    这么做是为了规避「BM25 的无界分」与「余弦相似度的 0-1 分」量纲不可比。
    RRF 对每一路只取名次：``score(doc) = Σ 1 / (k + rank)``。
    """

    name = "hybrid"

    def __init__(
        self, primary: Retriever, secondary: Retriever, k: int = 60, top_k: int = 4
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.k = k
        self.top_k = top_k
        # 观测用：融合后每条命中由哪几路共同支撑。单路命中的可信度低于两路命中，
        # 这个字段让「两路印证」变成可断言的事实，而不是一句宣传语。
        self.last_matched: dict[str, list[str]] = {}
        self.last_trace: FusionTrace | None = None

    def search(self, query: str, k: int | None = None) -> list[tuple[Chunk, float]]:
        limit = k or self.top_k
        rankings: list[tuple[str, list[Chunk]]] = []
        for retriever in (self.primary, self.secondary):
            hits = retriever.search(query, limit)
            if hits:
                rankings.append((retriever.name, [c for c, _ in hits]))

        fused: defaultdict[str, float] = defaultdict(float)
        store: dict[str, Chunk] = {}
        matched: defaultdict[str, list[str]] = defaultdict(list)
        trace = FusionTrace(paths=[name for name, _ in rankings])

        for name, chunks in rankings:
            for rank, chunk in enumerate(chunks):
                # ★ 合并键 = 内容指纹。用 chunk.id 之类的路径内标识会永远对不上，
                #   两路印证就会静默失效（上个项目就是这么栽的）。
                key = chunk.fingerprint
                store[key] = chunk
                fused[key] += 1.0 / (self.k + rank + 1)
                if name not in matched[key]:
                    matched[key].append(name)

        trace.counter = {store[key].heading: len(names) for key, names in matched.items()}
        self.last_trace = trace
        self.last_matched = dict(matched)

        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], store[pair[0]].heading))
        return [(store[key], score) for key, score in ordered[:limit]]

    def stats(self) -> dict:
        return {
            "name": self.name,
            "k": self.k,
            "primary": self.primary.stats(),
            "secondary": self.secondary.stats(),
        }


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def build_bm25_index(chunks: list[Chunk] | None = None) -> BM25Index:
    return BM25Index(chunks if chunks is not None else load_corpus())


def build_retriever(
    backend: str | None = None,
    chunks: list[Chunk] | None = None,
) -> Retriever:
    """按配置建检索器。hybrid 在缺少向量后端时**明确降级**并记录原因。"""
    settings = get_settings()
    backend = backend or settings.retrieval_backend
    corpus = chunks if chunks is not None else load_corpus()

    if backend == "bm25":
        return BM25Index(corpus)

    if backend == "vector":
        index = ChromaVectorIndex(corpus)
        if not index.available():
            return _degraded(corpus, index.last_error)
        return index

    if backend == "hybrid":
        vector = ChromaVectorIndex(corpus)
        if not vector.available():
            return _degraded(corpus, vector.last_error)
        return HybridRetriever(
            BM25Index(corpus), vector, k=settings.rrf_k, top_k=settings.retrieval_top_k
        )

    raise ValueError(f"未知检索后端：{backend}")


_FALLBACK_REASON: str | None = None


def _degraded(corpus: list[Chunk], reason: str | None) -> BM25Index:
    global _FALLBACK_REASON
    _FALLBACK_REASON = reason or "向量后端不可用"
    return BM25Index(corpus)


def fallback_reason() -> str | None:
    return _FALLBACK_REASON


def to_hits(results: list[tuple[Chunk, float]], matched_by: dict[str, list[str]] | None = None) -> list[DocHit]:
    out: list[DocHit] = []
    for chunk, score in results:
        out.append(
            DocHit(
                source=chunk.source,
                heading=chunk.heading,
                text=chunk.text,
                score=round(float(score), 6),
                matched_by=(matched_by or {}).get(chunk.fingerprint, []),
            )
        )
    return out
