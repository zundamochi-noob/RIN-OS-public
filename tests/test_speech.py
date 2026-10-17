"""core/speech.py（/completionの返答の読み上げ）と core/voicevox.py の話者照合のテスト。
generate と play はモックにする（VOICEVOXも音もデバイスも使わない）。"""
import os
import sys
import unittest
from unittest import mock

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, llm_anthropic, speech, voicevox  # noqa: E402

AUDIO = object()  # 合成結果の代わり（playもモックなので中身は見ない）


class SpeechBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.generate = mock.AsyncMock(return_value=AUDIO)
        self.play = mock.MagicMock()
        for target, new in (("core.speech.generate", self.generate), ("core.speech.play", self.play),
                            ("core.speech.COMPLETION_SPEAK", True)):
            p = mock.patch(target, new)
            p.start()
            self.addCleanup(p.stop)

    async def drain(self):
        while speech._tasks:
            await next(iter(speech._tasks))


class SplitTests(unittest.TestCase):
    def test_splits_on_sentence_ends_and_newlines(self):
        self.assertEqual(speech.split_sentences("おはよう。元気？うん！\n次の行"), ["おはよう。", "元気？", "うん！", "次の行"])

    def test_keeps_repeated_punctuation_together(self):
        self.assertEqual(speech.split_sentences("えっ！？本当！！"), ["えっ！？", "本当！！"])

    def test_text_without_sentence_end_is_one_sentence(self):
        self.assertEqual(speech.split_sentences("句点のない文"), ["句点のない文"])

    def test_blank_and_empty_give_nothing(self):
        self.assertEqual(speech.split_sentences(""), [])
        self.assertEqual(speech.split_sentences(" \n　\n"), [])
        self.assertEqual(speech.split_sentences(None), [])


class SpeakTests(SpeechBase):
    async def test_speaks_each_sentence_in_order(self):
        speech.schedule_speak("一つ目。二つ目。三つ目。")
        await self.drain()
        self.assertEqual([c.args[0] for c in self.generate.call_args_list], ["一つ目。", "二つ目。", "三つ目。"])
        self.assertEqual(self.play.call_count, 3)

    async def test_speak_returns_spoken(self):
        self.assertEqual(await speech._speak(["一つ。", "二つ。"], speech._generation), "spoken")

    async def test_failed_synthesis_stops_without_playing_and_does_not_raise(self):
        self.generate.return_value = None
        self.assertEqual(await speech._speak(["一つ。", "二つ。"], speech._generation), "failed")
        self.play.assert_not_called()

    async def test_play_error_is_swallowed(self):
        self.play.side_effect = RuntimeError("no device")
        self.assertEqual(await speech._speak(["一つ。"], speech._generation), "failed")

    async def test_newer_reply_drops_the_rest(self):
        gen = speech._generation + 1
        speech._generation = gen
        # 1文目の再生中に新しい返答が予約された、という状況を作る
        self.play.side_effect = lambda audio: setattr(speech, "_generation", gen + 1)
        self.assertEqual(await speech._speak(["一つ。", "二つ。", "三つ。"], gen), "superseded")
        self.assertEqual(self.play.call_count, 1)

    async def test_playback_is_serialized(self):
        active, peak = [0], [0]

        def slow_play(audio):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            active[0] -= 1

        self.play.side_effect = slow_play
        speech.schedule_speak("一つ。二つ。")
        speech.schedule_speak("三つ。四つ。")
        await self.drain()
        self.assertEqual(peak[0], 1)


class SkipTests(SpeechBase):
    async def assert_not_spoken(self, reply):
        speech.schedule_speak(reply)
        await self.drain()
        self.generate.assert_not_called()
        self.play.assert_not_called()

    async def test_disabled(self):
        with mock.patch("core.speech.COMPLETION_SPEAK", False):
            await self.assert_not_spoken("返答です。")

    async def test_empty_replies(self):
        for reply in ("", "  \n", None):
            await self.assert_not_spoken(reply)

    async def test_fixed_messages(self):
        for reply in (llm_anthropic.BUDGET_MESSAGE, llm_anthropic.ERROR_MESSAGE):
            await self.assert_not_spoken(reply)

    async def test_schedule_never_raises(self):
        with mock.patch("core.speech.split_sentences", side_effect=RuntimeError("x")):
            speech.schedule_speak("返答です。")  # 例外が出なければよい


class FakeClient:
    """httpx.AsyncClient の代わり。GET /speakers の応答を固定する"""

    def __init__(self, speakers=None, error=None):
        self.speakers, self.error = speakers, error

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if self.error:
            raise self.error
        return httpx.Response(200, json=self.speakers, request=httpx.Request("GET", url))


SPEAKERS = [{"name": "冥鳴ひまり", "styles": [{"name": "ノーマル", "id": 14}]},
            {"name": "WhiteCUL", "styles": [{"name": "たのしい", "id": 24}]}]


class CheckSpeakerTests(unittest.IsolatedAsyncioTestCase):
    async def result(self, client, speaker_id=14):
        with mock.patch("core.voicevox.httpx.AsyncClient", client), mock.patch("core.voicevox.SPEAKER_ID", speaker_id):
            with self.assertLogs("core.voicevox", level="DEBUG") as cm:
                out = await voicevox.check_speaker()
        return out, cm.output

    async def test_match(self):
        out, logs = await self.result(FakeClient(SPEAKERS))
        self.assertEqual(out, "ok")
        self.assertEqual(len(logs), 1)

    async def test_wrong_speaker_warns_once(self):
        out, logs = await self.result(FakeClient(SPEAKERS), speaker_id=24)
        self.assertEqual(out, "mismatch")
        self.assertEqual(len(logs), 1)
        self.assertIn("WARNING", logs[0])

    async def test_unknown_id_warns(self):
        out, logs = await self.result(FakeClient(SPEAKERS), speaker_id=999)
        self.assertEqual(out, "mismatch")
        self.assertIn("WARNING", logs[0])

    async def test_voicevox_not_running_is_skipped_without_raising(self):
        out, logs = await self.result(FakeClient(error=httpx.ConnectError("refused")))
        self.assertEqual(out, "skipped")
        self.assertEqual(len(logs), 1)
        self.assertNotIn("WARNING", logs[0])

    async def test_bad_response_is_skipped(self):
        out, logs = await self.result(FakeClient(speakers={"detail": "x"}))
        self.assertEqual(out, "skipped")
        self.assertEqual(len(logs), 1)


class GenerateParamsTests(unittest.IsolatedAsyncioTestCase):
    """generate() が synthesis に渡す audio_query の内容（VOICEVOXへは接続しない）"""

    # audio_query が返す初期値の代わり。上書きされたら値が変わるので見分けられる
    QUERY = {"speedScale": 1.3, "pitchScale": 0.07, "intonationScale": 1.0, "volumeScale": 1.0,
             "prePhonemeLength": 0.11, "postPhonemeLength": 0.13, "outputSamplingRate": 24000,
             "accent_phrases": []}

    class Client:
        def __init__(self, query):
            self.query, self.synthesis_body = query, None

        def __call__(self, *a, **k):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, params=None, json=None):
            if url.endswith("/audio_query"):
                return httpx.Response(200, json=dict(self.query), request=httpx.Request("POST", url))
            self.synthesis_body = json
            return httpx.Response(200, content=b"wav", request=httpx.Request("POST", url))

    async def test_only_volume_and_intonation_are_overridden(self):
        client = self.Client(self.QUERY)
        wav = mock.MagicMock(return_value=([0.0, 0.1, 0.2], 24000))
        with mock.patch("core.voicevox.httpx.AsyncClient", client), mock.patch("core.voicevox.sf.read", wav):
            audio = await voicevox.generate("テスト")
        self.assertIsNotNone(audio)
        sent = client.synthesis_body
        self.assertEqual(sent["volumeScale"], 0.5)
        self.assertEqual(sent["intonationScale"], 1.2)
        # それ以外は audio_query の応答のまま
        for key, value in self.QUERY.items():
            if key not in ("volumeScale", "intonationScale"):
                self.assertEqual(sent[key], value, key)

    def test_config_constants(self):
        self.assertEqual(config.VOICE_VOLUME_SCALE, 0.5)
        self.assertEqual(config.VOICE_INTONATION_SCALE, 1.2)


class ConfigTests(unittest.TestCase):
    def test_speaker_and_device_defaults(self):
        self.assertEqual(config.SPEAKER_ID, 14)
        self.assertEqual((config.SPEAKER_NAME, config.SPEAKER_STYLE_NAME), ("冥鳴ひまり", "ノーマル"))
        self.assertIsNone(config.AUDIO_DEVICE)

    def test_play_uses_default_device_when_none(self):
        with mock.patch("core.voicevox.sd") as sd:
            voicevox.play(AUDIO)
        self.assertIsNone(sd.play.call_args.kwargs["device"])
        sd.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
