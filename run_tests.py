#!/usr/bin/env python3
"""根目录测试运行器：兼容 `python3 -I run_tests.py`。

隔离模式（-I）下 sys.path 不含当前目录，这里显式加入仓库根，
并以 discover 方式加载 tests/，避免在资料目录内解释执行。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py",
                            top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
