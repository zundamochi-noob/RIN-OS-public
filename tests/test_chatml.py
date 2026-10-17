"""core/chatml.py の単体テスト。合成データのみ（実際のログ本文は使わない）。

実行: rin\\ で  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.chatml import decompose  # noqa: E402

S, E = "<|im_start|>", "<|im_end|>"


def turn(body: str, label: str = "") -> str:
    """Mate Engineが作る1ターン。履歴ターンはロール名が空"""
    return f"{S}{label}\n{body}{E}\n"


SYSTEM = turn("キャラ設定AAA\r\n二行目BBB", "system")


class ChatmlTests(unittest.TestCase):
    def test_warmup_shape_last_assistant_closed(self):
        # Warmup形: 最後がassistantで閉じ、生成開始タグが無い
        prompt = SYSTEM + turn("u1") + turn("") + turn("u2") + turn("a2")
        d = decompose(prompt)
        self.assertEqual(d.status, "warmup")
        self.assertEqual(d.messages[-1]["role"], "assistant")
        self.assertFalse(d.info["open_tail"])
        self.assertEqual(d.latest_user, "")

    def test_production_shape_open_tag_at_end(self):
        # 本番形: 履歴 + 今回のuser + 開きタグ
        prompt = SYSTEM + turn("u1") + turn("") + turn("u2") + turn("a2") + turn("今回の発言") + f"{S}\n"
        d = decompose(prompt)
        self.assertEqual(d.status, "ok")
        self.assertTrue(d.info["open_tail"])
        self.assertEqual(d.messages[-1], {"role": "user", "content": "今回の発言"})
        self.assertEqual(d.latest_user, "今回の発言")
        # 空assistantとその直前のuserはセットで消え、中身のある組は残る
        self.assertEqual([m["role"] for m in d.messages], ["user", "assistant", "user"])
        self.assertEqual(d.messages[0]["content"], "u2")
        self.assertEqual(d.info["dropped_pairs"], 1)

    def test_observed_shape_ten_history_turns(self):
        # 観測した構造に近い合成データ: 履歴10ターン（空返答3組 + 中身あり2組）+ 今回のuser + 開きタグ
        hist = ""
        for i in range(3):
            hist += turn(f"挨拶{i}") + turn("")
        hist += turn("挨拶3") + turn("返答3") + turn("挨拶4") + turn("返答4")
        prompt = SYSTEM + hist + turn("新しい発言") + f"{S}\n"
        d = decompose(prompt)
        self.assertEqual(d.status, "ok")
        self.assertEqual(d.info["turns_parsed"], 11)
        self.assertEqual(d.info["dropped_pairs"], 3)
        self.assertEqual(d.info["roles"], "UAUAU")
        self.assertEqual(d.info["merged"], 0)

    def test_crlf_normalized_in_system(self):
        d = decompose(SYSTEM + turn("u") + f"{S}\n")
        self.assertNotIn("\r", d.system)
        self.assertEqual(d.system, "キャラ設定AAA\n二行目BBB")

    def test_no_system_turn(self):
        d = decompose(turn("u") + f"{S}\n")
        self.assertEqual(d.status, "ok")
        self.assertEqual(d.system, "")
        self.assertFalse(d.info["has_system"])

    def test_roles_are_assigned_by_position_not_label(self):
        # ラベルが付いていても（逆でも）位置で割り当てる
        prompt = SYSTEM + turn("u", "assistant") + turn("a", "user") + turn("u2", "xyz") + f"{S}assistant\n"
        d = decompose(prompt)
        self.assertEqual(d.status, "ok")
        self.assertEqual([m["role"] for m in d.messages], ["user", "assistant", "user"])
        self.assertEqual(d.info["open_tail"], True)

    def test_empty_user_dropped_then_same_role_merged(self):
        # 空のuserは単独で捨てられ、その結果assistantが連続したら "\n" で結合する
        prompt = SYSTEM + turn("u1") + turn("x") + turn("") + turn("y") + turn("u2") + f"{S}\n"
        d = decompose(prompt)
        self.assertEqual(d.status, "ok")
        self.assertEqual([m["role"] for m in d.messages], ["user", "assistant", "user"])
        self.assertEqual(d.messages[1]["content"], "x\ny")
        self.assertEqual(d.info["merged"], 1)
        self.assertEqual(d.info["dropped_empty_user"], 1)

    def test_empty_last_user_is_not_sent(self):
        # 今回のuser発言が空なら、最後がuserにならないのでAPIを呼ばない
        prompt = SYSTEM + turn("u1") + turn("a1") + turn("") + f"{S}\n"
        d = decompose(prompt)
        self.assertEqual(d.status, "warmup")

    def test_only_system_and_open_tag(self):
        d = decompose(SYSTEM + f"{S}\n")
        self.assertEqual(d.status, "warmup")
        self.assertEqual(d.messages, [])

    def test_no_tags_falls_back_to_single_user_message(self):
        prompt = "ただの文字列。タグが無い"
        d = decompose(prompt)
        self.assertEqual(d.status, "fallback")
        self.assertEqual(d.system, "")
        self.assertEqual(d.messages, [{"role": "user", "content": prompt}])
        self.assertEqual(d.info["reason"], "no_tags")

    def test_malformed_unterminated_turn_falls_back(self):
        prompt = SYSTEM + f"{S}\n本文だけでim_endが無い" + turn("次")
        d = decompose(prompt)
        self.assertEqual(d.status, "fallback")
        self.assertEqual(d.messages[0]["content"], prompt)

    def test_text_before_first_tag_falls_back(self):
        d = decompose("前置き" + SYSTEM + turn("u"))
        self.assertEqual(d.status, "fallback")
        self.assertEqual(d.info["reason"], "text_before_first_tag")

    def test_system_not_first_falls_back(self):
        d = decompose(turn("u") + turn("later", "system"))
        self.assertEqual(d.status, "fallback")
        self.assertEqual(d.info["reason"], "system_not_first")

    def test_latest_user_is_pre_merge_text(self):
        prompt = SYSTEM + turn("u1") + turn("x") + turn("") + turn("y") + turn("最新") + f"{S}\n"
        d = decompose(prompt)
        self.assertEqual(d.latest_user, "最新")

    def test_info_contains_no_body_text(self):
        prompt = SYSTEM + turn("秘密の本文XYZ") + turn("返答QRS") + turn("今回MNO") + f"{S}\n"
        d = decompose(prompt)
        dump = repr(d.info)
        for secret in ("秘密の本文XYZ", "返答QRS", "今回MNO", "キャラ設定AAA"):
            self.assertNotIn(secret, dump)

    def test_fallback_info_contains_no_body_text(self):
        d = decompose("秘密の本文XYZ")
        self.assertNotIn("秘密の本文XYZ", repr(d.info))

    # ---- 先頭がuserになることの保証 ----
    def test_leading_assistant_is_removed(self):
        # 空のuserが捨てられた結果、先頭がassistantになる形 → 先頭のassistantを除去して先頭をuserにする
        prompt = SYSTEM + turn("") + turn("先頭のassistant") + turn("今回の発言") + f"{S}\n"
        d = decompose(prompt)
        self.assertEqual(d.status, "ok")
        self.assertEqual(d.messages, [{"role": "user", "content": "今回の発言"}])
        self.assertEqual(d.info["dropped_leading_assistant"], 1)
        self.assertEqual(d.info["roles"], "U")
        self.assertEqual(d.latest_user, "今回の発言")

    def test_leading_assistant_removed_keeps_following_pairs(self):
        prompt = (SYSTEM + turn("") + turn("先頭のa") + turn("u2") + turn("a2")
                  + turn("今回") + f"{S}\n")
        d = decompose(prompt)
        self.assertEqual(d.status, "ok")
        self.assertEqual([m["role"] for m in d.messages], ["user", "assistant", "user"])
        self.assertEqual(d.messages[0]["content"], "u2")
        self.assertEqual(d.info["dropped_leading_assistant"], 1)

    def test_messages_empty_after_leading_assistant_removal_is_warmup(self):
        # 除去の結果messagesが空になる形 → warmup扱い（APIを呼ばない）
        prompt = SYSTEM + turn("") + turn("先頭のassistantだけ")
        d = decompose(prompt)
        self.assertEqual(d.status, "warmup")
        self.assertEqual(d.messages, [])
        self.assertEqual(d.info["dropped_leading_assistant"], 1)
        self.assertEqual(d.latest_user, "")

    def test_last_not_user_after_leading_assistant_removal_is_warmup(self):
        # 先頭除去後も最後がassistantなら、既存どおりwarmup扱い
        prompt = SYSTEM + turn("") + turn("a0") + turn("u1") + turn("a1")
        d = decompose(prompt)
        self.assertEqual(d.status, "warmup")
        self.assertEqual([m["role"] for m in d.messages], ["user", "assistant"])
        self.assertEqual(d.info["dropped_leading_assistant"], 1)

    def test_normal_shapes_are_unchanged_by_leading_guard(self):
        # 通常形（先頭がuser）は変わらない。除去は起きない
        prod = SYSTEM + turn("u1") + turn("a1") + turn("今回の発言") + f"{S}\n"
        d = decompose(prod)
        self.assertEqual(d.status, "ok")
        self.assertEqual(d.info["dropped_leading_assistant"], 0)
        self.assertEqual(d.messages, [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "今回の発言"},
        ])
        warm = decompose(SYSTEM + turn("u1") + turn("a1"))
        self.assertEqual(warm.status, "warmup")
        self.assertEqual(warm.info["dropped_leading_assistant"], 0)
        self.assertEqual(warm.info["roles"], "UA")

    def test_every_ok_result_starts_and_ends_with_user(self):
        # 様々な形で、status=="ok" なら必ず 先頭=user・最後=user
        bodies = [("", "a"), ("u", ""), ("", ""), ("u", "a"), ("", "a")]
        for n in range(0, 5):
            for combo in __import__("itertools").product(bodies, repeat=n):
                hist = "".join(turn(u) + turn(a) for u, a in combo)
                d = decompose(SYSTEM + hist + turn("今回") + f"{S}\n")
                if d.status == "ok":
                    self.assertEqual(d.messages[0]["role"], "user")
                    self.assertEqual(d.messages[-1]["role"], "user")
                else:
                    self.fail(f"今回のuser発言が最後にあるのにstatus={d.status}: {combo}")


if __name__ == "__main__":
    unittest.main()
