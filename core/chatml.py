"""
Mate Engine（LLMUnity）が /completion に送ってくるChatML形式のpromptを、
system と messages（user/assistant交互）に分解する純関数。外部状態を持たない。

観測した2つの形（本文は扱わない。構造のみ）:
  - Warmup（n_predict=0）: system + 履歴。最後のターンがassistantで閉じており、生成開始タグが無い
  - 本番（n_predict=-1）: system + 履歴 + 今回のuser発言 + 開きっぱなしの "<|im_start|>\\n"（生成開始）
  ロール名はWarmup/本番とも空（<|im_start|>の直後が改行）。そのためロールは位置で割り当てる。

分解の手順:
  1. <|im_start|>role\\n本文<|im_end|>\\n でターン分割。改行はCRLFをLFに正規化する
  2. ロール名が system の先頭ターンを system にする
  3. 残りは位置でロールを割り当てる（奇数番目=user、偶数番目=assistant）。空ターンを除く前に割り当てる
  4. 末尾の本文もim_endも無い開きタグは生成開始として捨てる
  5. 中身が空のassistantは、直前のuserとセットで捨てる（結合はしない）。
     それでも同ロールが連続する場合だけ "\\n" で結合する
  6. 先頭がassistantなら、その1つを除去する（messagesの先頭は必ずuser）
  7. messagesが空、または最後がuserでなければ status="warmup"（APIを呼ばない）。
     パースできなければ status="fallback"（prompt全体をuser1通として扱う）
"""
import re
from dataclasses import dataclass, field

START = "<|im_start|>"
END = "<|im_end|>"


class ChatmlError(ValueError):
    """ChatMLとして解釈できないpromptに対して送出する。reason は構造のみの短い識別子"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Decomposition:
    system: str
    messages: list
    # "ok": 最後がuser。APIを呼んでよい / "warmup": 最後がuserでない。APIを呼ばない
    # "fallback": パース失敗。prompt全体をuser1通にして従来どおり渡す
    status: str
    # 結合前の最後のuser発言（RAGのクエリ用）。なければ空文字
    latest_user: str = ""
    # 構造情報のみ（本文は含めない）。ログ出力用
    info: dict = field(default_factory=dict)


def _split_turns(text: str):
    """(turns, open_tail) を返す。turnsは[(role_label, body), ...]。open_tailは末尾に生成開始タグがあったか"""
    if START not in text:
        raise ChatmlError("no_tags")
    chunks = text.split(START)
    if chunks[0].strip():
        raise ChatmlError("text_before_first_tag")

    turns = []
    open_tail = False
    last_index = len(chunks) - 1
    for i in range(1, len(chunks)):
        label, sep, rest = chunks[i].partition("\n")
        is_last = i == last_index
        if not sep:
            # "<|im_start|>assistant" のように改行が無い。末尾の開きタグ以外は不正
            if is_last and END not in chunks[i]:
                open_tail = True
                continue
            raise ChatmlError("missing_newline_after_role")
        if END in rest:
            body, _, tail = rest.partition(END)
            if tail.strip():
                raise ChatmlError("text_after_end_tag")
            turns.append((label.strip(), body))
        elif is_last and not rest.strip():
            open_tail = True
        else:
            raise ChatmlError("unterminated_turn")
    return turns, open_tail


def _fallback(prompt: str, reason: str) -> Decomposition:
    return Decomposition(
        system="",
        messages=[{"role": "user", "content": prompt}],
        status="fallback",
        latest_user="",
        info={"status": "fallback", "reason": reason, "prompt_chars": len(prompt)},
    )


def decompose(prompt: str) -> Decomposition:
    text = prompt.replace("\r\n", "\n")
    try:
        turns, open_tail = _split_turns(text)
    except ChatmlError as e:
        return _fallback(prompt, e.reason)

    system = ""
    if turns and turns[0][0].lower() == "system":
        system = turns[0][1].strip()
        turns = turns[1:]
    if any(label.lower() == "system" for label, _ in turns):
        return _fallback(prompt, "system_not_first")

    # 位置でロールを割り当てる（空ターンを除く前）
    assigned = [
        ("user" if i % 2 == 0 else "assistant", body.strip())
        for i, (_, body) in enumerate(turns)
    ]
    latest_user = ""
    for role, body in reversed(assigned):
        if role == "user":
            latest_user = body
            break

    # 空のassistantは直前のuserとセットで捨てる。空のuserは単独で捨てる
    kept = []
    dropped_pairs = 0
    dropped_empty_user = 0
    for role, body in assigned:
        if role == "assistant" and not body:
            if kept and kept[-1][0] == "user":
                kept.pop()
                dropped_pairs += 1
            continue
        if role == "user" and not body:
            dropped_empty_user += 1
            continue
        kept.append((role, body))

    # それでも同ロールが連続する場合だけ結合する
    messages = []
    merged = 0
    for role, body in kept:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + body
            merged += 1
        else:
            messages.append({"role": role, "content": body})

    # 先頭がassistantなら除去する。Anthropic APIのmessagesは先頭がuserである必要があるとされる
    # （公式で断定できていないため防御的に対応）。ここまでで同ロール連続は結合済みなので、除去するのは先頭1つだけ
    dropped_leading_assistant = 0
    if messages and messages[0]["role"] == "assistant":
        messages.pop(0)
        dropped_leading_assistant = 1

    # 空になった場合や、最後がuserでない場合はAPIを呼ばない（Warmupと同じ扱い）
    status = "ok" if messages and messages[-1]["role"] == "user" else "warmup"
    info = {
        "status": status,
        "has_system": bool(system),
        "system_chars": len(system),
        "turns_parsed": len(assigned),
        "open_tail": open_tail,
        "dropped_pairs": dropped_pairs,
        "dropped_empty_user": dropped_empty_user,
        "merged": merged,
        "dropped_leading_assistant": dropped_leading_assistant,
        "roles": "".join("U" if m["role"] == "user" else "A" for m in messages),
        "chars": [len(m["content"]) for m in messages],
    }
    return Decomposition(system=system, messages=messages, status=status,
                         latest_user=latest_user if status == "ok" else "", info=info)
