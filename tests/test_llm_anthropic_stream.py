"""core/llm_anthropic.py の stream() の句読点分割のテスト。APIは呼ばず、合成の文字列チャンク列を流す。

実行: rin\\ で  python -m unittest discover -s tests -v
"""
import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import llm, llm_anthropic, ollama  # noqa: E402
from helpers import isolate_usage  # noqa: E402


class FakeStream:
    """anthropic の messages.stream(...) が返す非同期コンテキストマネージャの代役"""

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()

    async def get_final_message(self):
        return SimpleNamespace(
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=2),
            content=[SimpleNamespace(type="text")],
        )


class FakeClient:
    def __init__(self, chunks):
        self.calls = []
        self.messages = SimpleNamespace(stream=self._stream)
        self._chunks = chunks

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self._chunks)


async def collect(agen):
    return [x async for x in agen]


# SDKが返しうる、複数文字をまとめたチャンク列（合成。個人情報なし）
CHUNKS = ["こんにちは。今日は", "いい天気ですね！散歩でも", "どうですか？", "それでは"]


async def anthropic_stream_via_facade(system, messages, max_tokens=None, temperature=None, timeout=120):
    """llm.stream の代わりに llm_anthropic.stream を直接使う（ollama._stream_chat から呼ばせる用）"""
    async for c in llm_anthropic.stream(system, messages, max_tokens, temperature, timeout):
        yield c


class SplitTests(unittest.TestCase):
    def test_splits_at_each_punctuation_as_single_chunk(self):
        self.assertEqual(llm_anthropic._split_sentence_end("こんにちは。今日は！"),
                         ["こんにちは", "。", "今日は", "！"])

    def test_edge_cases(self):
        f = llm_anthropic._split_sentence_end
        self.assertEqual(f(""), [])
        self.assertEqual(f("句読点なし"), ["句読点なし"])
        self.assertEqual(f("。"), ["。"])
        self.assertEqual(f("。あ"), ["。", "あ"])
        self.assertEqual(f("え！？"), ["え", "！", "？"])  # 連続は1文字ずつ
        self.assertEqual(f("は、はい"), ["は、はい"])      # 「、」は対象外（ollama.pyの集合に無い）

    def test_pieces_rejoin_to_original(self):
        for text in ["", "あ", "。", "あ。い！う？え", "。。！！？？", "終わり？"]:
            self.assertEqual("".join(llm_anthropic._split_sentence_end(text)), text)

    def test_every_punctuation_piece_matches_ollama_condition(self):
        # ollama._stream_chat の条件 `chunk and chunk in 句読点` に必ず一致する
        句読点 = "。！？"
        for text in ["あ。い！う？", "。！？", "！？"]:
            for p in llm_anthropic._split_sentence_end(text):
                if p in 句読点:
                    self.assertTrue(p and p in 句読点)
                else:
                    self.assertFalse(any(ch in 句読点 for ch in p))

    def test_punctuation_set_matches_ollama_py(self):
        # ollama.py の `句読点 = "..."` と同じ集合であること（ずれたら検知する）
        with open(os.path.join(ROOT, "core", "ollama.py"), encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'句読点\s*=\s*"([^"]*)"', src)
        self.assertIsNotNone(m, "ollama.py に句読点の定義が見つからない")
        self.assertEqual(llm_anthropic.SENTENCE_END, m.group(1))


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate_usage(self)  # 使用量の保存先を一時フォルダにする（実際の集計ファイルを汚さない）

    async def test_stream_yields_punctuation_as_standalone_chunks(self):
        client = FakeClient(CHUNKS)
        with mock.patch.object(llm_anthropic, "_get_client", return_value=client):
            pieces = await collect(llm_anthropic.stream("sys", [{"role": "user", "content": "q"}], 100, None, 30))
        self.assertEqual("".join(pieces), "".join(CHUNKS))
        for p in pieces:
            if any(ch in "。！？" for ch in p):
                self.assertEqual(len(p), 1, f"句読点が単独になっていない: {p!r}")
        self.assertEqual(pieces[:4], ["こんにちは", "。", "今日は", "いい天気ですね"])

    async def test_request_has_thinking_disabled_and_no_sampling_params(self):
        client = FakeClient(["はい。"])
        with mock.patch.object(llm_anthropic, "_get_client", return_value=client):
            await collect(llm_anthropic.stream("sys", [{"role": "user", "content": "q"}], None, 0.5, 30))
        kw = client.calls[0]
        self.assertEqual(kw["thinking"], {"type": "disabled"})
        self.assertEqual(kw["model"], "claude-haiku-5-5")
        self.assertEqual(kw["max_tokens"], 300)  # 未指定なら/input経路と同じ上限
        for k in ("temperature", "top_p", "top_k"):
            self.assertNotIn(k, kw)

    async def test_stream_chat_flushes_per_sentence_with_split(self):
        # 実物の ollama._stream_chat の区切りロジックに通して、文単位でyieldされることを確認する
        client = FakeClient(CHUNKS)
        with mock.patch.object(llm_anthropic, "_get_client", return_value=client), \
                mock.patch.object(llm, "stream", anthropic_stream_via_facade):
            sentences = await collect(ollama._stream_chat("q", []))
        self.assertEqual(sentences, ["こんにちは。", "今日はいい天気ですね！", "散歩でもどうですか？", "それでは"])

    async def test_without_split_stream_chat_would_not_flush_until_end(self):
        # 対照: 分割しない場合は、区切りが働かず最後に1回だけyieldされる（これが問題だった）
        client = FakeClient(CHUNKS)
        with mock.patch.object(llm_anthropic, "_get_client", return_value=client), \
                mock.patch.object(llm_anthropic, "_split_sentence_end", lambda t: [t]), \
                mock.patch.object(llm, "stream", anthropic_stream_via_facade):
            sentences = await collect(ollama._stream_chat("q", []))
        self.assertEqual(sentences, ["".join(CHUNKS)])

    async def test_consecutive_punctuation_flushes_each(self):
        client = FakeClient(["え！？そう"])
        with mock.patch.object(llm_anthropic, "_get_client", return_value=client), \
                mock.patch.object(llm, "stream", anthropic_stream_via_facade):
            sentences = await collect(ollama._stream_chat("q", []))
        self.assertEqual(sentences, ["え！", "？", "そう"])


if __name__ == "__main__":
    unittest.main()
