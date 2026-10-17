"""テスト共通の補助。実際の使用量ファイル（LOGS_DIR 配下の usage）を汚さないための隔離。"""
import os
import shutil
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import usage  # noqa: E402


def isolate_usage(testcase) -> str:
    """usage.py の保存先を一時フォルダに差し替える。テスト終了時に元に戻して削除する。一時フォルダのパスを返す"""
    tmp = tempfile.mkdtemp(prefix="rin_usage_test_")
    testcase.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    for name, value in (("LOGS_DIR", tmp), ("USAGE_DIR", os.path.join(tmp, "usage"))):
        p = mock.patch.object(usage, name, value)
        p.start()
        testcase.addCleanup(p.stop)
    return tmp
