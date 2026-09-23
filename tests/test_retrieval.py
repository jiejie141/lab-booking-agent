"""检索层：BM25、融合与「融合键必须用内容指纹」这条教训的回归测试。"""

from __future__ import annotations

import pytest

from lagent.knowledge.retriever import (
    BM25Index,
    Chunk,
    HybridRetriever,
    Retriever,
    build_retriever,
    load_corpus,
    tokenize,
)


class TestTokenize:
    def test_cjk_becomes_bigrams(self):
        assert "配平" in tokenize("对称配平")

    def test_ascii_words_kept(self):
        assert "rpm" in tokenize("转速 10000 RPM")

    def test_single_cjk_char_kept(self):
        assert "泵" in tokenize("泵")


class TestCorpus:
    def test_split_by_heading(self):
        chunks = load_corpus()
        assert len(chunks) >= 8
        headings = {c.heading for c in chunks}
        assert "离心类设备使用规范" in headings
        assert all(c.text.strip() for c in chunks)

    def test_fingerprint_is_content_based(self):
        a = Chunk(source="S", heading="H", text="T")
        b = Chunk(source="S", heading="H", text="T")
        c = Chunk(source="S", heading="H", text="T2")
        # 内容相同的两个不同对象必须得到同一个指纹
        assert a.fingerprint == b.fingerprint
        assert a is not b
        # 内容变了指纹必须变
        assert a.fingerprint != c.fingerprint


class TestBM25:
    def test_finds_centrifuge_rules(self):
        index = BM25Index(load_corpus())
        hits = index.search("离心机 配平 转速", k=3)
        assert hits
        assert hits[0][0].heading == "离心类设备使用规范"

    def test_finds_training_rules(self):
        index = BM25Index(load_corpus())
        hits = index.search("准入资质 培训", k=3)
        assert any("培训" in c.heading for c, _ in hits)

    def test_scores_descend(self):
        index = BM25Index(load_corpus())
        scores = [score for _, score in index.search("安全 规范", k=5)]
        assert scores == sorted(scores, reverse=True)

    def test_empty_query_returns_nothing(self):
        index = BM25Index(load_corpus())
        assert index.search("", k=3) == []
        assert index.search("zzzzqqq", k=3) == []

    def test_stats_shape(self):
        stats = BM25Index(load_corpus()).stats()
        assert stats["name"] == "bm25"
        assert stats["chunks"] > 0 and stats["vocab"] > 0


class _FakeRetriever(Retriever):
    """按预设顺序返回命中的假检索器，用来把融合逻辑单独测出来。"""

    def __init__(self, name: str, chunks: list[Chunk]) -> None:
        self.name = name
        self._chunks = chunks

    def search(self, query: str, k: int = 4):
        return [(chunk, 1.0 - i * 0.1) for i, chunk in enumerate(self._chunks[:k])]


class TestRRFFusion:
    """★ 回归测试：融合键必须是内容指纹。

    上个项目用路径内的 chunk.id 做合并键（BM25 侧是 ``source#N``、向量侧是
    ``instance_id#N``），两边永远不相等，于是「两路互相印证」静默失效、
    同一篇文档还会重复出现。而当时的测试全绿 —— 因为它只断言了分数过阈值，
    单路 rank0 的分数恰好也能过。

    这里改为断言两件**能真正抓到那个 bug** 的事：
      1. 同一篇文档无论被几路召回，结果里只出现一次；
      2. 两路都命中的文档，分数严格高于单路 rank0 的 1/(k+1)。
    """

    def _make(self):
        shared_a = Chunk(source="规范", heading="共同命中", text="两路都会召回的文档")
        # 关键：两个**不同对象、相同内容**，模拟「同一文档被两路各自召回」
        a_from_bm25 = Chunk(source=shared_a.source, heading=shared_a.heading, text=shared_a.text)
        a_from_vector = Chunk(source=shared_a.source, heading=shared_a.heading, text=shared_a.text)
        only_bm25 = Chunk(source="规范", heading="只在 BM25", text="词面命中")
        only_vector = Chunk(source="规范", heading="只在向量", text="语义命中")

        primary = _FakeRetriever("bm25", [a_from_bm25, only_bm25])
        secondary = _FakeRetriever("vector", [a_from_vector, only_vector])
        return HybridRetriever(primary, secondary, k=60)

    def test_same_document_appears_once(self):
        fused = self._make().search("任意", k=10)
        texts = [chunk.text for chunk, _ in fused]
        assert len(texts) == len(set(texts)), f"融合结果出现重复文档：{texts}"

    def test_two_path_hit_beats_single_path(self):
        hybrid = self._make()
        fused = hybrid.search("任意", k=10)
        single_path_rank0 = 1.0 / (60 + 1)  # 单路 rank0 的 RRF 分数

        shared = next(chunk for chunk, _ in fused if chunk.text == "两路都会召回的文档")
        shared_score = next(score for chunk, score in fused if chunk is shared)
        assert shared_score > single_path_rank0 * 1.5
        assert shared_score == pytest.approx(2 * single_path_rank0, rel=1e-6)

    def test_matched_by_records_both_paths(self):
        hybrid = self._make()
        fused = hybrid.search("任意", k=10)
        shared = next(chunk for chunk, _ in fused if chunk.text == "两路都会召回的文档")
        assert sorted(hybrid.last_matched[shared.fingerprint]) == ["bm25", "vector"]

        single = next(chunk for chunk, _ in fused if chunk.text == "词面命中")
        assert hybrid.last_matched[single.fingerprint] == ["bm25"]

    def test_both_ranked_first_when_equal_rank(self):
        fused = self._make().search("任意", k=10)
        assert fused[0][0].text == "两路都会召回的文档"

    def test_empty_paths_are_tolerated(self):
        hybrid = HybridRetriever(
            _FakeRetriever("bm25", []), _FakeRetriever("vector", []), k=60
        )
        assert hybrid.search("任意", k=5) == []

    def test_one_path_empty_still_works(self):
        chunk = Chunk(source="规范", heading="仅一路", text="内容")
        hybrid = HybridRetriever(
            _FakeRetriever("bm25", [chunk]), _FakeRetriever("vector", []), k=60
        )
        fused = hybrid.search("任意", k=5)
        assert len(fused) == 1
        assert hybrid.last_matched[chunk.fingerprint] == ["bm25"]


class TestFactory:
    def test_bm25_always_available(self):
        index = build_retriever("bm25")
        assert isinstance(index, BM25Index)

    def test_hybrid_degrades_without_vector_backend(self):
        """没装 chromadb 时 hybrid 必须**明确降级**而不是抛异常或假装可用。"""
        index = build_retriever("hybrid")
        if not isinstance(index, HybridRetriever):
            assert isinstance(index, BM25Index)
            from lagent.knowledge.retriever import fallback_reason

            assert fallback_reason() is not None

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError):
            build_retriever("nope")
