"""core/api.py の /completion 処理（_complete_for_mate_engine）のテスト。
合成のChatML promptを使い、LLM呼び出しとRAG検索は差し替える（APIもChromaDBも使わない）。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import api, context, ollama  # noqa: E402
from core.config import REPLY_MAX_TOKENS  # noqa: E402

S, E = "<|im_start|>", "<|im_end|>"


def turn(body, label=""):
    return f"{S}{label}\n{body}{E}\n"


PERSONA = "あなたは「テスト太郎」という合成のキャラです。簡潔に話します。"
SYSTEM = turn(PERSONA, "system")
HIST = turn("やあ") + turn("") + turn("元気？") + turn("ぼちぼち")
PRODUCTION = SYSTEM + HIST + turn("今回の発言") + f"{S}\n"
WARMUP_SHAPE = SYSTEM + HIST
HITS = [{"text": "（昨日の会話）合成の記憶A"}, {"text": "（3日前の会話）合成の記憶B"}]


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.complete = mock.AsyncMock(return_value="返答です。")
        self.search = mock.AsyncMock(return_value=HITS)
        # 会話の記録は差し替える（実際の conversations フォルダとChromaDBに書かないため）
        self.schedule_record = mock.MagicMock()
        # 読み上げも差し替える（VOICEVOXも音もデバイスも使わないため）
        self.schedule_speak = mock.MagicMock()
        for target, new in (("core.api.llm.complete", self.complete), ("core.api.search_memory", self.search),
                            ("core.api.record.schedule_record", self.schedule_record),
                            ("core.api.speech.schedule_speak", self.schedule_speak)):
            p = mock.patch(target, new)
            p.start()
            self.addCleanup(p.stop)

    def req(self, prompt, n_predict=-1):
        return api.CompletionRequest(prompt=prompt, stream=False, n_predict=n_predict)

    # ---- Warmup ----
    async def test_warmup_by_n_predict_zero_skips_llm_and_rag(self):
        self.assertEqual(await api._complete_for_mate_engine(self.req(PRODUCTION, n_predict=0)), "")
        self.complete.assert_not_called()
        self.search.assert_not_called()

    async def test_warmup_by_shape_last_assistant_skips_llm_and_rag(self):
        self.assertEqual(await api._complete_for_mate_engine(self.req(WARMUP_SHAPE, n_predict=None)), "")
        self.complete.assert_not_called()
        self.search.assert_not_called()

    # ---- system分離・日付・表記ルール・RAG ----
    async def test_system_is_separated_and_gets_date_rules_and_memory(self):
        out = await api._complete_for_mate_engine(self.req(PRODUCTION))
        self.assertEqual(out, "返答です。")
        system, messages = self.complete.call_args.args[:2]
        # system: キャラ設定が先頭にそのまま入り、その後に 日付 → 表記ルール → RAGの記憶 の順で並ぶ
        self.assertTrue(system.startswith(PERSONA))
        order = [system.index(x) for x in (PERSONA, "【現在日時】", "【表記のルール】", "【過去の記憶（参考）】")]
        self.assertEqual(order, sorted(order))
        self.assertIn("合成の記憶A", system)
        # messages: ChatMLのタグやsystemは含まれず、user/assistantが交互で、先頭も末尾もuser
        for m in messages:
            self.assertNotIn(S, m["content"])
            self.assertNotIn(PERSONA, m["content"])
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[-1], {"role": "user", "content": "今回の発言"})

    async def test_rag_query_is_latest_user_utterance(self):
        await api._complete_for_mate_engine(self.req(PRODUCTION))
        self.search.assert_awaited_once_with("今回の発言")

    async def test_rules_are_always_added_even_without_memory_hits(self):
        self.search.return_value = []
        await api._complete_for_mate_engine(self.req(PRODUCTION))
        system = self.complete.call_args.args[0]
        self.assertIn("【表記のルール】", system)
        self.assertNotIn("【過去の記憶（参考）】", system)

    async def test_rag_failure_continues_without_memory(self):
        self.search.side_effect = RuntimeError("chroma down")
        out = await api._complete_for_mate_engine(self.req(PRODUCTION))
        self.assertEqual(out, "返答です。")
        system = self.complete.call_args.args[0]
        self.assertIn("【表記のルール】", system)
        self.assertNotIn("【過去の記憶（参考）】", system)

    async def test_max_tokens_for_n_predict_values(self):
        for n_predict, expected in ((-1, REPLY_MAX_TOKENS), (None, REPLY_MAX_TOKENS), (50, 50)):
            self.complete.reset_mock()
            await api._complete_for_mate_engine(self.req(PRODUCTION, n_predict=n_predict))
            self.assertEqual(self.complete.call_args.kwargs["max_tokens"], expected, n_predict)

    # ---- 読み上げ・記録の呼び出し ----
    async def test_reply_is_recorded_and_spoken(self):
        await api._complete_for_mate_engine(self.req(PRODUCTION))
        self.schedule_record.assert_called_once_with("今回の発言", "返答です。")
        self.schedule_speak.assert_called_once_with("返答です。")

    async def test_voice_off_skips_speaking_but_still_records(self):
        with mock.patch.object(api, "voice_enabled", False):
            await api._complete_for_mate_engine(self.req(PRODUCTION))
        self.schedule_speak.assert_not_called()
        self.schedule_record.assert_called_once()

    async def test_warmup_and_fallback_are_not_spoken_or_recorded(self):
        await api._complete_for_mate_engine(self.req(PRODUCTION, n_predict=0))
        await api._complete_for_mate_engine(self.req("タグのない合成の文字列"))
        self.schedule_speak.assert_not_called()
        self.schedule_record.assert_not_called()

    async def test_fallback_for_non_chatml_prompt_passes_prompt_as_single_user_message(self):
        await api._complete_for_mate_engine(self.req("タグのない合成の文字列"))
        args = self.complete.call_args.args
        self.assertEqual(args[0], "")
        self.assertEqual(args[1], [{"role": "user", "content": "タグのない合成の文字列"}])
        self.search.assert_not_called()


class StyleRuleTests(unittest.TestCase):
    def test_rules_text_covers_the_four_requirements(self):
        text = ollama.style_rules()
        self.assertIn("括弧書き", text)
        self.assertIn("絵文字", text)
        self.assertIn("顔文字", text)
        self.assertIn("「！？」", text)
        self.assertIn("「！！」", text)
        self.assertIn("記号だけの文", text)
        self.assertLess(len(text), 200)  # 長くしすぎない

    def test_input_path_system_message_is_unchanged(self):
        # /input経路（_build_system_message）には表記ルールを入れない
        self.assertNotIn("表記のルール", ollama._build_system_message("suffix"))
        self.assertNotIn("表記のルール", ollama.compose_system("base", "suffix"))


class DiaryExclusionTests(unittest.IsolatedAsyncioTestCase):
    async def _excluded_for(self, backend):
        with mock.patch.object(context, "LLM_BACKEND", backend), \
                mock.patch.object(context, "rag_query", return_value=[]) as q:
            await context.search_memory("クエリ")
        return q.call_args.args[2]

    async def test_diary_excluded_only_for_external_backends(self):
        self.assertEqual(await self._excluded_for("ollama"), set())
        self.assertEqual(await self._excluded_for("anthropic"), {"diary"})
        self.assertEqual(await self._excluded_for("bedrock"), {"diary"})


if __name__ == "__main__":
    unittest.main()
