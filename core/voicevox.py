import httpx
import sounddevice as sd
import soundfile as sf
import numpy as np
import io
import logging

from core.config import (VOICEVOX_URL, SPEAKER_ID, SPEAKER_NAME, SPEAKER_STYLE_NAME, AUDIO_DEVICE, SAMPLE_RATE,
                         VOICE_VOLUME_SCALE, VOICE_INTONATION_SCALE)

logger = logging.getLogger(__name__)


async def check_speaker() -> str:
    """SPEAKER_ID が SPEAKER_NAME/SPEAKER_STYLE_NAME と一致するかを GET /speakers で照合し、結果をログ1行に書く。
    VOICEVOXが未起動・応答なしなら照合をスキップする。例外は出さない（起動を失敗させない）。
    戻り値: 'ok' / 'mismatch' / 'skipped'"""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            res = await client.get(f"{VOICEVOX_URL}/speakers")
            res.raise_for_status()
            speakers = res.json()
        if not isinstance(speakers, list):
            raise ValueError("想定外の応答")
    except Exception as e:
        logger.info("[voicevox] 話者の照合をスキップしました（VOICEVOXに接続できません: %s）", type(e).__name__)
        return "skipped"
    for s in speakers:
        for style in s.get("styles", []):
            if style.get("id") == SPEAKER_ID:
                if s.get("name") == SPEAKER_NAME and style.get("name") == SPEAKER_STYLE_NAME:
                    logger.info("[voicevox] 話者を確認しました: id=%s は %s（%s）", SPEAKER_ID, SPEAKER_NAME, SPEAKER_STYLE_NAME)
                    return "ok"
                logger.warning("[voicevox] 話者が設定と一致しません: id=%s は %s（%s）、期待は %s（%s）",
                               SPEAKER_ID, s.get("name"), style.get("name"), SPEAKER_NAME, SPEAKER_STYLE_NAME)
                return "mismatch"
    logger.warning("[voicevox] 話者が見つかりません: id=%s（期待は %s（%s））", SPEAKER_ID, SPEAKER_NAME, SPEAKER_STYLE_NAME)
    return "mismatch"


async def generate(text: str):
    if not text or not text.strip():
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            query = await client.post(
                f"{VOICEVOX_URL}/audio_query",
                params={"text": text, "speaker": SPEAKER_ID}
            )
            query_json = query.json()
            # 上書きするのは音量と抑揚だけ。速度・音高・前後の無音などは audio_query の初期値のまま使う
            query_json["volumeScale"] = VOICE_VOLUME_SCALE
            query_json["intonationScale"] = VOICE_INTONATION_SCALE

            audio = await client.post(
                f"{VOICEVOX_URL}/synthesis",
                params={"speaker": SPEAKER_ID},
                json=query_json
            )

            data, samplerate = sf.read(io.BytesIO(audio.content))
            ratio = SAMPLE_RATE / samplerate
            new_length = int(len(data) * ratio)
            resampled = np.interp(
                np.linspace(0, len(data), new_length),
                np.arange(len(data)),
                data
            )
            return resampled
    except httpx.ConnectError:
        # VOICEVOXが起動していない場合は音声なしで続行
        return None
    except Exception as e:
        print(f"[voicevox] エラー: {e}")
        return None


def play(audio_data):
    sd.play(audio_data, SAMPLE_RATE, device=AUDIO_DEVICE)
    sd.wait()
