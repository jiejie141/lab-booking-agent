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
        """没装 chromadb 时 hybrid 必须**明确降级**而不是抛异常或假装可用。

        这条用例原先写成 ``if not isinstance(index, HybridRetriever): ...`` ——
        那是个**恒真断言**：装了 chromadb 时条件为假、整个函数体被跳过，
        用例照样通过，于是「启用向量后端」这条路径实际上**零覆盖**。
        现在改成两条互斥且**都必须成立**的断言：无论 chromadb 在不在，
        二选一必然被执行，不会再有静默通过的余地。
        """
        from lagent.knowledge.retriever import ChromaVectorIndex, fallback_reason

        # 用实际情况（而不是猜测）判断向量后端在不在：
        # ChromaVectorIndex 构造失败时 available() 为 False。
        vector_ok = ChromaVectorIndex(load_corpus()).available()
        index = build_retriever("hybrid")

        if vector_ok:
            # 装了向量后端：必须真的用上，且不许报降级原因。
            assert isinstance(index, HybridRetriever), (
                "chromadb 可用时 hybrid 回退成了 BM25 —— 启用路径没有被真正执行"
            )
            assert fallback_reason() is None
        else:
            # 没装向量后端：必须明确降级到 BM25 并给出原因，而不是抛异常。
            assert isinstance(index, BM25Index)
            assert not isinstance(index, HybridRetriever)
            assert fallback_reason() is not None

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError):
            build_retriever("nope")

    def test_repeated_construction_stays_usable(self):
        """同一个进程里连续建两次向量索引，第二次不许失败。

        chromadb 的 ``EphemeralClient()`` 在同一个进程里是**共享实例**，所以
        第二次构造时集合已经存在。原先用 ``create_collection`` 就会抛出
        ``Collection [lab_safety_rules] already exists``，而这个异常被
        ``except Exception`` 吞掉之后**不会报错**，只会让 ``available()`` 变 False。

        后果不是「一次构造失败」，而是**只有第一个请求走 hybrid，此后全部静默退回
        BM25，而 HTTP 依然 200**。这条用例钉的就是这个：连建三次都必须可用。

        这也是 ``build_retriever`` 每个请求都重建索引（见 ``api.retrieve``）时
        必须成立的前提。
        """
        from lagent.knowledge.retriever import ChromaVectorIndex

        try:
            import chromadb  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"本环境没装 chromadb：{exc}")

        corpus = load_corpus()
        # 第一轮也要断言，不许「第一轮不行就 skip」—— 那样正好绕过本用例要抓的
        # 场景：别的用例（或上一个请求）已经把集合建出来了，这一轮才撞名。
        for round_no in (1, 2, 3):
            index = ChromaVectorIndex(corpus)
            assert index.available(), (
                f"第 {round_no} 次构造向量索引失败：{index.last_error}"
                " —— 集合跨实例共享，create_collection 撞名后不该让整条路静默降级"
            )
            hits = index.search("离心机 配平", k=3)
            assert hits, "重建出来的索引搜不到任何东西，等于降级"

    def test_successful_build_clears_stale_fallback_reason(self):
        """降级原因不许比事实活得久。

        上一次构造失败会把 ``_FALLBACK_REASON`` 写成那条旧错误；如果成功路径
        不清它，接口就会在之后**每一次正常**请求里继续把 ``degraded_reason``
        报给前端，控制台据此显示「已降级」—— 而实际上一切正常。
        """
        from lagent.knowledge import retriever as R

        R._FALLBACK_REASON = "InternalError: 上一轮留下的旧原因"
        build_retriever("bm25")
        assert R.fallback_reason() is None, "显式选 bm25 不是降级，不该留着旧原因"

        R._FALLBACK_REASON = "InternalError: 上一轮留下的旧原因"
        index = build_retriever("hybrid")
        if isinstance(index, HybridRetriever):
            assert R.fallback_reason() is None, "hybrid 建成之后仍报旧降级原因"
        else:
            assert R.fallback_reason() is not None, "真的降级了就必须有原因"
