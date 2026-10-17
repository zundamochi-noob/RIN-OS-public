"""core/llm_anthropic.py の安全策（max_tokens丸め・タイムアウト・リトライ・月額上限・エラー時の固定メッセージ）のテスト。
APIは呼ばない（偽クライアント）。使用量の保存先は一時フォルダに隔離する。"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm_anthropic, llm_ollama, usage  # noqa: E402
from core.config import ANTHROPIC_MAX_TOKENS_CAP, ANTHROPIC_MAX_RETRIES, ANTHROPIC_TIMEOUT_SEC, REPLY_MAX_TOKENS  # noqa: E402
from helpers import isolate_usage  # noqa: E402

SECRET_BODY = "本文に相当する秘密の文字列XYZ"


class ApiError(Exception):
    """anthropic.APIStatusError の代役（status_code と request_id を持つ）"""

    def __init__(self, message, status_code=500, request_id="req_test"):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


def fake_message(text="了解です。", input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[SimpleNamespace(type="text", text=text)],
    )


class FakeStreamCtx:
    def __init__(self, chunks, error=None, usage_tokens=(10, 5)):
        self._chunks, self._error, self._usage = chunks, error, usage_tokens

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for c in self._chunks:
                yield c
            if self._error:
                raise self._error
        return gen()

    @property
    def current_message_snapshot(self):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=self._usage[0], output_tokens=self._usage[1]))

    async def get_final_message(self):
        return fake_message("".join(self._chunks), *self._usage)


class FakeClient:
    def __init__(self, reply="了解です。", error=None, stream_chunks=None, stream_error=None, usage_tokens=(10, 5)):
        self.calls = []
        self._reply, self._error = reply, error
        self._chunks, self._stream_error, self._usage = stream_chunks or [reply], stream_error, usage_tokens
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    async def _create(self, **kw):
        self.calls.append(kw)
        if self._error:
            raise self._error
        return fake_message(self._reply, *self._usage)

    def _stream(self, **kw):
        self.calls.append(kw)
        if self._error:
            raise self._error
        return FakeStreamCtx(self._chunks, self._stream_error, self._usage)


async def collect(agen):
    return [x async for x in agen]


MSGS = [{"role": "user", "content": "合成の発言"}]


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        isolate_usage(self)

    def _use(self, client):
        p = mock.patch.object(llm_anthropic, "_get_client", return_value=client)
        p.start()
        self.addCleanup(p.stop)
        return client

    # ---- max_tokens / timeout / retry ----
    async def test_max_tokens_is_clamped_to_cap(self):
        c = self._use(FakeClient())
        await llm_anthropic.complete("sys", MSGS, 100_000, None, 30)
        await llm_anthropic.complete("sys", MSGS, 15, None, 30)
        await llm_anthropic.complete("sys", MSGS, None, None, 30)
        await collect(llm_anthropic.stream("sys", MSGS, 5_000, None, 30))
        self.assertEqual([k["max_tokens"] for k in c.calls],
                         [ANTHROPIC_MAX_TOKENS_CAP, 15, min(REPLY_MAX_TOKENS, ANTHROPIC_MAX_TOKENS_CAP), ANTHROPIC_MAX_TOKENS_CAP])
        self.assertLessEqual(ANTHROPIC_MAX_TOKENS_CAP, 1000)

    async def test_timeout_is_capped_by_config(self):
        c = self._use(FakeClient())
        await llm_anthropic.complete("sys", MSGS, 10, None, 120)
        await llm_anthropic.complete("sys", MSGS, 10, None, 10)
        self.assertEqual([k["timeout"] for k in c.calls], [ANTHROPIC_TIMEOUT_SEC, 10])

    def test_client_is_built_with_explicit_timeout_and_minimal_retries(self):
        fake_module_client = mock.MagicMock()
        with mock.patch.dict(os.environ, {"RIN_ANTHROPIC_API_KEY": "dummy-key-for-test"}), \
                mock.patch("anthropic.AsyncAnthropic", fake_module_client), \
                mock.patch.object(llm_anthropic, "_client", None):
            llm_anthropic._get_client()
        kwargs = fake_module_client.call_args.kwargs
        self.assertEqual(kwargs["max_retries"], ANTHROPIC_MAX_RETRIES)
        self.assertEqual(kwargs["timeout"], ANTHROPIC_TIMEOUT_SEC)
        self.assertEqual(kwargs["api_key"], "dummy-key-for-test")
        self.assertLessEqual(ANTHROPIC_MAX_RETRIES, 1)  # リトライは最小限

    async def test_missing_api_key_returns_fixed_message_without_secret(self):
        env = {k: v for k, v in os.environ.items() if k != "RIN_ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(llm_anthropic, "_client", None):
            with self.assertLogs("core.llm_anthropic", level="ERROR") as logs:
                out = await llm_anthropic.complete("sys", MSGS, 10, None, 30)
        self.assertEqual(out, llm_anthropic.ERROR_MESSAGE)
        self.assertIn("RIN_ANTHROPIC_API_KEY", "\n".join(logs.output))

    # ---- 月額上限 ----
    async def test_over_budget_does_not_call_api_and_returns_fixed_message(self):
        c = self._use(FakeClient())
        with mock.patch.object(usage, "ANTHROPIC_MONTHLY_BUDGET_USD", 0.0001), \
                mock.patch.object(llm_ollama, "complete", side_effect=AssertionError("ollamaへ自動フォールバックしてはいけない")), \
                mock.patch.object(llm_ollama, "stream", side_effect=AssertionError("ollamaへ自動フォールバックしてはいけない")):
            usage.record(10_000, 10_000)  # 0.006 > 0.0001
            with self.assertLogs("core.llm_anthropic", level="WARNING") as logs:
                out = await llm_anthropic.complete("sys", MSGS, 10, None, 30)
            pieces = await collect(llm_anthropic.stream("sys", MSGS, 10, None, 30))
        self.assertEqual(out, llm_anthropic.BUDGET_MESSAGE)
        self.assertEqual(pieces, [llm_anthropic.BUDGET_MESSAGE])
        self.assertEqual(c.calls, [])  # APIは1回も呼ばれていない
        self.assertTrue(any("月額上限" in line for line in logs.output))

    async def test_budget_message_ends_with_sentence_end_for_tts(self):
        self.assertIn(llm_anthropic.BUDGET_MESSAGE[-1], llm_anthropic.SENTENCE_END)
        self.assertIn(llm_anthropic.ERROR_MESSAGE[-1], llm_anthropic.SENTENCE_END)

    async def test_budget_resets_when_month_changes(self):
        c = self._use(FakeClient())
        with mock.patch.object(usage, "ANTHROPIC_MONTHLY_BUDGET_USD", 0.0001):
            with mock.patch.object(usage, "_month", return_value="2026-10"):
                usage.record(10_000, 10_000)
                self.assertEqual(await llm_anthropic.complete("sys", MSGS, 10, None, 30), llm_anthropic.BUDGET_MESSAGE)
            with mock.patch.object(usage, "_month", return_value="2026-11"):
                self.assertEqual(await llm_anthropic.complete("sys", MSGS, 10, None, 30), "了解です。")
        self.assertEqual(len(c.calls), 1)

    async def test_unreadable_usage_blocks_api_call(self):
        c = self._use(FakeClient())
        with mock.patch.object(usage, "LOGS_DIR", os.path.join(os.sep, "no_such_dir_for_test")):
            with self.assertLogs("core.llm_anthropic", level="ERROR"):
                out = await llm_anthropic.complete("sys", MSGS, 10, None, 30)
        self.assertEqual(out, llm_anthropic.ERROR_MESSAGE)
        self.assertEqual(c.calls, [])

    async def test_usage_is_recorded_after_complete_and_stream(self):
        self._use(FakeClient(usage_tokens=(120, 30)))
        await llm_anthropic.complete("sys", MSGS, 10, None, 30)
        await collect(llm_anthropic.stream("sys", MSGS, 10, None, 30))
        state = usage.load()
        self.assertEqual((state["calls"], state["input_tokens"], state["output_tokens"]), (2, 240, 60))

    # ---- APIエラー ----
    async def test_api_error_returns_fixed_message_and_logs_no_body(self):
        self._use(FakeClient(error=ApiError(f"エラー詳細 {SECRET_BODY}", status_code=529, request_id="req_abc")))
        with self.assertLogs("core.llm_anthropic", level="ERROR") as logs:
            out = await llm_anthropic.complete("sys", MSGS, 10, None, 30)
        text = "\n".join(logs.output)
        self.assertEqual(out, llm_anthropic.ERROR_MESSAGE)
        self.assertNotIn(SECRET_BODY, text)
        self.assertIn("ApiError", text)
        self.assertIn("status=529", text)
        self.assertIn("req_abc", text)
        self.assertEqual(usage.load()["calls"], 0)  # 失敗した呼び出しは積算しない

    async def test_stream_error_before_output_yields_fixed_message(self):
        self._use(FakeClient(error=ApiError(SECRET_BODY)))
        with self.assertLogs("core.llm_anthropic", level="ERROR") as logs:
            pieces = await collect(llm_anthropic.stream("sys", MSGS, 10, None, 30))
        self.assertEqual(pieces, [llm_anthropic.ERROR_MESSAGE])
        self.assertNotIn(SECRET_BODY, "\n".join(logs.output))

    async def test_stream_error_after_partial_output_does_not_append_message(self):
        self._use(FakeClient(stream_chunks=["途中まで。", "続き"], stream_error=ApiError(SECRET_BODY),
                             usage_tokens=(50, 4)))
        with self.assertLogs("core.llm_anthropic", level="ERROR") as logs:
            pieces = await collect(llm_anthropic.stream("sys", MSGS, 10, None, 30))
        self.assertEqual("".join(pieces), "途中まで。続き")
        self.assertNotIn(llm_anthropic.ERROR_MESSAGE, pieces)
        self.assertNotIn(SECRET_BODY, "\n".join(logs.output))
        # 中断されても、その時点までの使用量は積算される
        state = usage.load()
        self.assertEqual((state["calls"], state["input_tokens"], state["output_tokens"]), (1, 50, 4))

    async def test_no_body_in_info_logs_of_normal_call(self):
        self._use(FakeClient(reply=f"返答 {SECRET_BODY}。"))
        with self.assertLogs("core.llm_anthropic", level="INFO") as logs:
            await llm_anthropic.complete(f"system {SECRET_BODY}", [{"role": "user", "content": SECRET_BODY}], 10, None, 30)
        self.assertNotIn(SECRET_BODY, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
