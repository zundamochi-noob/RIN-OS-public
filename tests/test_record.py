"""core/record.py（/completion の会話を記憶に記録する）のテスト。
合成データと一時フォルダだけを使う。実際の conversations / diary フォルダと ChromaDB には触れない
（index_markdown_file は差し替えるか、偽のコレクションを使う）。"""
import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import api, llm_anthropic, rag, record, vault  # noqa: E402

S, E = "<|im_start|>", "<|im_end|>"


def turn(body, label=""):
    return f"{S}{label}\n{body}{E}\n"


PERSONA = "あなたは合成のキャラです。"
HIST_USER, HIST_ASSISTANT = "履歴のuser発言XYZ", "履歴の返答QRS"
LATEST = "今回の発言MNO"
REPLY = "今回の返答ABC。"
PRODUCTION = turn(PERSONA, "system") + turn(HIST_USER) + turn(HIST_ASSISTANT) + turn(LATEST) + f"{S}\n"
WARMUP_SHAPE = turn(PERSONA, "system") + turn(HIST_USER) + turn(HIST_ASSISTANT)


class RecordBase(unittest.IsolatedAsyncioTestCase):
    """一時フォルダ・偽のインデックス・固定の時刻/時計を用意する共通の土台"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rin_record_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.index = mock.MagicMock(return_value=2)
        self.now = [1000.0]
        self.hhmm = ["09:15"]
        patches = [
            mock.patch.object(vault, "CONVERSATIONS_DIR", os.path.join(self.tmp, "conversations")),
            mock.patch.object(record, "index_markdown_file", self.index),
            mock.patch.object(record, "COMPLETION_RECORD", True),
            mock.patch.object(record, "COMPLETION_DEDUPE_SEC", 30),
            mock.patch.object(record, "_clock", lambda: self.now[0]),
            mock.patch.object(record, "_hhmm", lambda: self.hhmm[0]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        record._recent.clear()

    def log_path(self):
        d = os.path.join(self.tmp, "conversations")
        files = os.listdir(d) if os.path.isdir(d) else []
        return os.path.join(d, files[0]) if files else None

    def file_text(self):
        p = self.log_path()
        if not p:
            return None
        with open(p, encoding="utf-8") as f:
            return f.read()

    async def drain(self):
        """予約されたバックグラウンドタスクの完了を待つ"""
        while record._tasks:
            await asyncio.gather(*list(record._tasks), return_exceptions=True)


class RecordShapeTests(RecordBase):
    async def test_recorded_format_matches_input_path(self):
        self.assertEqual(await record.record_turn(LATEST, REPLY), "recorded")
        text = self.file_text()
        date = os.path.splitext(os.path.basename(self.log_path()))[0]
        self.assertEqual(text, f"# {date}\n\n## [09:15] User\n{LATEST}\n\n## [09:15] 凛\n{REPLY}\n\n")
        self.index.assert_called_once_with(self.log_path())

    async def test_matches_vault_append_conversation_output_exactly(self):
        # /input 経路が使う関数と同じ書式であること（同じ関数を使っている）
        other = os.path.join(self.tmp, "other")
        with mock.patch.object(vault, "CONVERSATIONS_DIR", other):
            vault.append_conversation("09:15", LATEST, REPLY)
        with open(os.path.join(other, os.listdir(other)[0]), encoding="utf-8") as f:
            expected = f.read()
        await record.record_turn(LATEST, REPLY)
        self.assertEqual(self.file_text(), expected)

    async def test_two_turns_are_appended_in_order_as_intact_pairs(self):
        await record.record_turn("発言1", "返答1。")
        self.hhmm[0] = "09:16"
        await record.record_turn("発言2", "返答2。")
        text = self.file_text()
        self.assertEqual(re.findall(r"(?m)^## \[(\d\d:\d\d)\] (User|凛)$", text),
                         [("09:15", "User"), ("09:15", "凛"), ("09:16", "User"), ("09:16", "凛")])

    async def test_concurrent_records_do_not_interleave(self):
        results = await asyncio.gather(*(record.record_turn(f"発言{i}", f"返答{i}。") for i in range(5)))
        self.assertEqual(results, ["recorded"] * 5)
        pairs = re.findall(r"## \[\d\d:\d\d\] User\n発言(\d)\n\n## \[\d\d:\d\d\] 凛\n返答(\d)。\n\n", self.file_text())
        self.assertEqual(len(pairs), 5)
        self.assertTrue(all(a == b for a, b in pairs), "発言と返答の組が入れ替わっていない")


class RecordSkipTests(RecordBase):
    async def test_skip_conditions_write_nothing(self):
        cases = {
            "empty_reply": (LATEST, ""),
            "empty_reply_ws": (LATEST, "  \n "),
            "empty_user": ("", REPLY),
            "fixed_message_budget": (LATEST, llm_anthropic.BUDGET_MESSAGE),
            "fixed_message_error": (LATEST, llm_anthropic.ERROR_MESSAGE),
        }
        for name, (u, r) in cases.items():
            res = await record.record_turn(u, r)
            self.assertTrue(res.startswith("skipped:"), name)
        self.assertIsNone(self.log_path())
        self.index.assert_not_called()

    async def test_disabled_flag_records_nothing(self):
        with mock.patch.object(record, "COMPLETION_RECORD", False):
            self.assertEqual(await record.record_turn(LATEST, REPLY), "skipped:disabled")
            record.schedule_record(LATEST, REPLY)
            await self.drain()
        self.assertIsNone(self.log_path())
        self.index.assert_not_called()

    async def test_duplicate_within_window_is_skipped_then_recorded_after_window(self):
        self.assertEqual(await record.record_turn(LATEST, REPLY), "recorded")
        self.now[0] += 29.9
        self.assertEqual(await record.record_turn(LATEST, "再送された返答。"), "skipped:duplicate")
        self.assertEqual(self.file_text().count("## [09:15] User"), 1)
        self.now[0] += 0.2   # 合計30.1秒 → 窓の外
        self.assertEqual(await record.record_turn(LATEST, "別の返答。"), "recorded")
        self.assertEqual(self.file_text().count("## [09:15] User"), 2)

    async def test_duplicate_is_judged_on_latest_user_text_only(self):
        await record.record_turn("同じ発言", "返答1。")
        self.assertEqual(await record.record_turn("  同じ発言 ", "返答2。"), "skipped:duplicate")   # 前後の空白は無視
        self.assertEqual(await record.record_turn("違う発言", "返答3。"), "recorded")

    async def test_simultaneous_identical_requests_record_once(self):
        # 同時に届いた同一リクエスト（再送）でも二重に書かない（予約の時点で印を付ける）
        record.schedule_record(LATEST, REPLY)
        record.schedule_record(LATEST, REPLY)
        await self.drain()
        self.assertEqual(self.file_text().count("## [09:15] User"), 1)


class RecordFailureTests(RecordBase):
    async def test_write_failure_returns_failed_and_logs_without_body(self):
        with mock.patch.object(record, "append_conversation", side_effect=OSError(f"disk full {LATEST}")):
            with self.assertLogs("core.record", level="WARNING") as logs:
                res = await record.record_turn(LATEST, REPLY)
        self.assertEqual(res, "failed:append")
        text = "\n".join(logs.output)
        self.assertEqual(len(logs.output), 1, "失敗のログは1行だけ")
        self.assertNotIn(LATEST, text)
        self.assertNotIn(REPLY, text)
        self.index.assert_not_called()

    async def test_append_returning_none_is_a_failure(self):
        with mock.patch.object(record, "append_conversation", return_value=None):
            with self.assertLogs("core.record", level="WARNING"):
                self.assertEqual(await record.record_turn(LATEST, REPLY), "failed:append")

    async def test_resend_is_recorded_after_a_write_failure(self):
        with mock.patch.object(record, "append_conversation", return_value=None):
            with self.assertLogs("core.record", level="WARNING"):
                await record.record_turn(LATEST, REPLY)
        self.assertEqual(await record.record_turn(LATEST, REPLY), "recorded")   # 印が外れているので記録される

    async def test_index_failure_keeps_the_text_and_does_not_double_append_on_resend(self):
        self.index.side_effect = RuntimeError(f"chroma error {LATEST}")
        with self.assertLogs("core.record", level="WARNING") as logs:
            res = await record.record_turn(LATEST, REPLY)
        self.assertEqual(res, "failed:index")
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(LATEST, "\n".join(logs.output))
        self.assertEqual(self.file_text().count("## [09:15] User"), 1)       # 追記は済んでいる
        self.assertEqual(await record.record_turn(LATEST, REPLY), "skipped:duplicate")   # 再送で二重に追記しない

    async def test_schedule_never_raises_even_if_everything_fails(self):
        with mock.patch.object(record, "append_conversation", side_effect=RuntimeError("boom")):
            record.schedule_record(LATEST, REPLY)    # 例外が外に出ない
            with self.assertLogs("core.record", level="WARNING"):
                await self.drain()
        with mock.patch.object(record, "_skip_reason", side_effect=RuntimeError("boom")):
            with self.assertLogs("core.record", level="WARNING"):
                record.schedule_record(LATEST, REPLY)   # 予約の段階の失敗も外に出ない

    async def test_logs_contain_no_body_on_success(self):
        with self.assertLogs("core.record", level="INFO") as logs:
            await record.record_turn(LATEST, REPLY)
        text = "\n".join(logs.output)
        self.assertNotIn(LATEST, text)
        self.assertNotIn(REPLY, text)
        self.assertIn("user=%d文字" % len(LATEST), text)


class ApiIntegrationTests(RecordBase):
    """api._complete_for_mate_engine から、実際の record を通した結果（LLMだけ差し替える）"""

    def setUp(self):
        super().setUp()
        self.complete = mock.AsyncMock(return_value=REPLY)
        for target, new in (("core.api.llm.complete", self.complete),
                            ("core.api.search_memory", mock.AsyncMock(return_value=[]))):
            p = mock.patch(target, new)
            p.start()
            self.addCleanup(p.stop)

    def req(self, prompt, n_predict=-1):
        return api.CompletionRequest(prompt=prompt, stream=False, n_predict=n_predict)

    async def test_reply_is_returned_and_only_latest_pair_is_recorded(self):
        out = await api._complete_for_mate_engine(self.req(PRODUCTION))
        self.assertEqual(out, REPLY)
        await self.drain()
        text = self.file_text()
        self.assertIn(f"## [09:15] User\n{LATEST}\n", text)
        self.assertIn(f"## [09:15] 凛\n{REPLY}\n", text)
        for old in (HIST_USER, HIST_ASSISTANT, PERSONA):
            self.assertNotIn(old, text, "Unityが送る履歴・キャラ設定は記録しない")

    async def test_warmup_and_fallback_are_not_recorded(self):
        self.assertEqual(await api._complete_for_mate_engine(self.req(PRODUCTION, n_predict=0)), "")
        self.assertEqual(await api._complete_for_mate_engine(self.req(WARMUP_SHAPE, n_predict=None)), "")
        await api._complete_for_mate_engine(self.req("ChatMLではない合成の文字列"))   # フォールバック
        await self.drain()
        self.assertIsNone(self.log_path())
        self.index.assert_not_called()

    async def test_fixed_messages_from_the_backend_are_not_recorded(self):
        for msg in (llm_anthropic.BUDGET_MESSAGE, llm_anthropic.ERROR_MESSAGE):
            self.complete.return_value = msg
            self.assertEqual(await api._complete_for_mate_engine(self.req(PRODUCTION)), msg)
        await self.drain()
        self.assertIsNone(self.log_path())

    async def test_resend_of_same_latest_utterance_is_recorded_once(self):
        await api._complete_for_mate_engine(self.req(PRODUCTION))
        await api._complete_for_mate_engine(self.req(PRODUCTION))
        await self.drain()
        self.assertEqual(self.file_text().count(f"## [09:15] User\n{LATEST}\n"), 1)

    async def test_recording_failure_does_not_affect_the_reply(self):
        with mock.patch.object(record, "append_conversation", side_effect=OSError("disk full")):
            with self.assertLogs("core.record", level="WARNING"):
                out = await api._complete_for_mate_engine(self.req(PRODUCTION))
                await self.drain()
        self.assertEqual(out, REPLY)

    async def test_disabled_flag_does_not_record_via_api(self):
        with mock.patch.object(record, "COMPLETION_RECORD", False):
            self.assertEqual(await api._complete_for_mate_engine(self.req(PRODUCTION)), REPLY)
            await self.drain()
        self.assertIsNone(self.log_path())

    async def test_record_is_scheduled_not_awaited_before_reply(self):
        # 返答が先に返り、記録はバックグラウンドのタスクとして予約される（返答時点ではまだ書かれていない）
        out = await api._complete_for_mate_engine(self.req(PRODUCTION))
        self.assertEqual(out, REPLY)
        self.assertTrue(record._tasks, "記録のタスクが予約されている")
        self.assertIsNone(self.log_path(), "返答を返した時点では、まだファイルに書かれていない")
        await self.drain()
        self.assertIsNotNone(self.log_path(), "タスクの完了後に書かれている")


class FakeCollection:
    """ChromaDB の代わり。get(ids) と upsert(...) だけを持つ"""

    def __init__(self):
        self.docs = {}
        self.upsert_calls = []

    def get(self, ids=None, where=None):
        return {"ids": [i for i in (ids or []) if i in self.docs]}

    def upsert(self, ids, embeddings, documents, metadatas):
        self.upsert_calls.append(list(ids))
        for i, d in zip(ids, documents):
            self.docs[i] = d


class ChunkIdTests(unittest.TestCase):
    """記録した会話が、既存の index_markdown_file で重複なく（新しいチャンクだけ）インデックスされること"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rin_chunk_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.col = FakeCollection()
        self.embedded = []
        p1 = mock.patch.object(rag, "get_collection", lambda: self.col)
        p2 = mock.patch.object(rag, "_embed_passages", lambda texts: (self.embedded.append(list(texts)) or [[0.0] for _ in texts]))
        p3 = mock.patch.object(vault, "CONVERSATIONS_DIR", os.path.join(self.tmp, "conversations"))
        for p in (p1, p2, p3):
            p.start()
            self.addCleanup(p.stop)

    def test_chunk_ids_are_unique_and_stable_when_appending(self):
        path = vault.append_conversation("09:15", "発言1", "返答1。")
        n1 = rag.index_markdown_file(path)
        ids_after_first = set(self.col.docs)
        path = vault.append_conversation("09:15", "発言2", "返答2。")   # 同じ分に2組目（同じ時刻ラベル）
        n2 = rag.index_markdown_file(path)
        self.assertEqual((n1, n2), (2, 2))
        self.assertEqual(len(self.col.docs), 4)
        self.assertTrue(ids_after_first <= set(self.col.docs), "既存のチャンクIDは変わらない")
        # 2回目のインデックスで埋め込まれたのは、新しい2チャンクだけ
        self.assertEqual(len(self.embedded), 2)
        self.assertEqual(len(self.embedded[1]), 2)
        # 全upsert呼び出しを通して、IDの重複がない
        all_ids = [i for call in self.col.upsert_calls for i in call]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_reindexing_the_same_file_embeds_nothing_new(self):
        path = vault.append_conversation("09:15", "発言1", "返答1。")
        rag.index_markdown_file(path)
        self.assertEqual(rag.index_markdown_file(path), 0)
        self.assertEqual(len(self.embedded), 1)

    def test_today_conversations_are_excluded_from_search_not_from_storage(self):
        # 当日の会話ログはRAGの検索対象外（履歴が担当）という既存の仕様を、コードの条件として確認する
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "rag.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertRegex(src, r'source_type == "conversation" and date == today')


if __name__ == "__main__":
    unittest.main()
