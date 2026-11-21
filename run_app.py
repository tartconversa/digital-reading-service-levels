#!/usr/bin/env python3
"""根目录启动器：兼容 `python3 -I run_app.py ...` 的隔离运行方式。

使用 -I（隔离模式）时 sys.path 不含当前目录，本启动器显式把仓库根加入路径，
避免在资料目录内解释执行导致的同名模块注入风险。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reading_accounting.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
