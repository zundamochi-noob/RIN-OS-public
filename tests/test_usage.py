"""core/usage.py（月次の使用量集計と月額上限）のテスト。保存先は一時フォルダに隔離する。"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import usage  # noqa: E402
from helpers import isolate_usage  # noqa: E402


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = isolate_usage(self)

    def _file(self, month):
        return os.path.join(self.tmp, "usage", f"anthropic-{month}.json")

    def test_cost_uses_standard_price_up_to_100k_prompt(self):
        # 入力10万 + 出力100万: (100000*0.10 + 1000000*0.50) / 1e6 = 0.51
        self.assertAlmostEqual(usage.call_cost_usd(100_000, 1_000_000), 0.51)

    def test_cost_uses_long_price_over_100k_prompt(self):
        # 10万を超えると長文用の単価（入力0.50 / 出力2.50）
        self.assertAlmostEqual(usage.call_cost_usd(100_001, 0), 100_001 * 0.50 / 1_000_000)
        self.assertAlmostEqual(usage.call_cost_usd(200_000, 1_000), (200_000 * 0.50 + 1_000 * 2.50) / 1_000_000)

    def test_record_accumulates_and_persists(self):
        with mock.patch.object(usage, "_month", return_value="2026-10"):
            usage.record(100, 20)
            state = usage.record(300, 40)
            self.assertEqual((state["calls"], state["input_tokens"], state["output_tokens"]), (2, 400, 60))
            self.assertAlmostEqual(state["cost_usd"], (400 * 0.10 + 60 * 0.50) / 1_000_000, places=9)
            # 別のプロセス/再読み込みでも同じ値（ファイルに永続化されている）
            self.assertEqual(usage.load()["calls"], 2)
        self.assertTrue(os.path.exists(self._file("2026-10")))
        self.assertFalse(os.path.exists(self._file("2026-10") + ".tmp"))

    def test_file_contains_only_counts_no_text(self):
        with mock.patch.object(usage, "_month", return_value="2026-10"):
            usage.record(5, 7)
        with open(self._file("2026-10"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(set(data), {"month", "calls", "input_tokens", "output_tokens", "cost_usd", "updated"})
        for key in ("calls", "input_tokens", "output_tokens", "cost_usd"):
            self.assertIsInstance(data[key], (int, float))

    def test_month_rollover_resets_totals(self):
        with mock.patch.object(usage, "_month", return_value="2026-10"):
            usage.record(1_000, 1_000)
            self.assertEqual(usage.load()["calls"], 1)
        with mock.patch.object(usage, "_month", return_value="2026-11"):
            self.assertEqual(usage.load()["calls"], 0)
            self.assertEqual(usage.month_cost_usd(), 0.0)
            self.assertFalse(usage.is_over_budget())
            usage.record(10, 10)
            self.assertEqual(usage.load()["calls"], 1)
        # 前月の集計は残り、影響を受けない
        self.assertEqual(usage.load("2026-10")["calls"], 1)

    def test_over_budget_detection(self):
        with mock.patch.object(usage, "_month", return_value="2026-10"), \
                mock.patch.object(usage, "ANTHROPIC_MONTHLY_BUDGET_USD", 0.005):
            self.assertFalse(usage.is_over_budget())
            usage.record(10_000, 0)  # 0.001
            self.assertFalse(usage.is_over_budget())
            usage.record(10_000, 10_000)  # +0.006 -> 0.007
            self.assertTrue(usage.is_over_budget())

    def test_exactly_at_budget_is_over(self):
        with mock.patch.object(usage, "_month", return_value="2026-10"), \
                mock.patch.object(usage, "ANTHROPIC_MONTHLY_BUDGET_USD", usage.call_cost_usd(1_000, 0)):
            usage.record(1_000, 0)
            self.assertTrue(usage.is_over_budget())

    def test_missing_logs_dir_is_unavailable(self):
        with mock.patch.object(usage, "LOGS_DIR", os.path.join(self.tmp, "no_such_drive")):
            with self.assertRaises(usage.UsageUnavailable):
                usage.is_over_budget()
            with self.assertRaises(usage.UsageUnavailable):
                usage.record(1, 1)

    def test_corrupt_files_are_unavailable(self):
        os.makedirs(os.path.join(self.tmp, "usage"))
        with mock.patch.object(usage, "_month", return_value="2026-10"):
            for content in ("{壊れたJSON", '{"calls": -1, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}',
                            '{"calls": 1}', "[]"):
                with open(self._file("2026-10"), "w", encoding="utf-8") as f:
                    f.write(content)
                with self.assertRaises(usage.UsageUnavailable, msg=content):
                    usage.is_over_budget()

    def test_write_failure_is_unavailable(self):
        with mock.patch.object(usage.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(usage.UsageUnavailable):
                usage.record(1, 1)


if __name__ == "__main__":
    unittest.main()
