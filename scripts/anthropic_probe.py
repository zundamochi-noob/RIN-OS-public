"""
Anthropic API（claude-haiku-5-5）の疎通・挙動確認用probe。
core/llm_anthropic.py の complete() と stream() を1回ずつ、合計2回だけ呼ぶ（課金あり）。

- 固定の短いプロンプト（個人情報なし）、max_tokensは小さく。
- SDKの自動リトライは0にして、呼び出し回数が2回を超えないようにする。
- APIキーは環境変数 RIN_ANTHROPIC_API_KEY から読む。値は表示しない。
- 出力: 返答テキスト、stop_reason、usage（入出力トークン）、thinkingブロックが返ってきたか。

実行: rin\\ で  python scripts\\anthropic_probe.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core import llm_anthropic  # noqa: E402
from core.config import ANTHROPIC_API_KEY_ENV, ANTHROPIC_MODEL  # noqa: E402

SYSTEM = "あなたは短く正確に答えます。"
PROMPT_COMPLETE = "17と23を足した数を、数字だけで答えてください。"
PROMPT_STREAM = "「疎通確認」とだけ返してください。"


def _show(label: str, text: str) -> None:
    info = llm_anthropic.last_call_info
    print(f"[{label}]")
    print("  返答テキスト:", repr(text))
    print("  stop_reason:", info.get("stop_reason"))
    print("  usage: input_tokens=%s output_tokens=%s" % (info.get("input_tokens"), info.get("output_tokens")))
    print("  content blocks:", info.get("block_types"))
    print("  thinkingブロックが返ってきたか:", any("thinking" in t for t in info.get("block_types", [])))


async def main() -> int:
    if not os.environ.get(ANTHROPIC_API_KEY_ENV):
        print(f"環境変数 {ANTHROPIC_API_KEY_ENV} が設定されていません。中止します（API呼び出しは0回）。")
        return 1
    llm_anthropic.MAX_RETRIES = 0  # クライアント生成前に上書き
    print(f"model={ANTHROPIC_MODEL} thinking=disabled（llm_anthropic の既定）")

    calls = 0
    try:
        calls += 1
        text = await llm_anthropic.complete(SYSTEM, [{"role": "user", "content": PROMPT_COMPLETE}],
                                            max_tokens=20, temperature=None, timeout=30)
        _show("1回目 complete()", text)

        calls += 1
        chunks = []
        async for c in llm_anthropic.stream(SYSTEM, [{"role": "user", "content": PROMPT_STREAM}],
                                            max_tokens=20, temperature=None, timeout=30):
            chunks.append(c)
        _show("2回目 stream()", "".join(chunks))
        print("  チャンク数:", len(chunks))
    except Exception as e:
        print(f"エラー（{calls}回目）: {type(e).__name__}: {str(e)[:300]}")
        return 2
    print(f"API呼び出し回数: {calls}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
