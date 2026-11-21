"""业务测试共用：在临时库上导入只读资料，保证不污染 materials/。"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reading_accounting.engine import Engine  # noqa: E402

MATERIALS = ROOT / "materials"


class FreshEngine:
    """每个用例一个全新临时库的引擎上下文。"""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "case.db"
        self.engine = Engine(self.db)

    def __enter__(self):
        self.engine.import_materials(MATERIALS)
        return self.engine

    def __exit__(self, *exc):
        self.engine.close()
        self._tmp.cleanup()
        return False
