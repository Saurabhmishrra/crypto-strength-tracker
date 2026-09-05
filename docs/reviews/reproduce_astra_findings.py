"""Run the regression coverage replacing the original defect reproductions."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_integrity.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
