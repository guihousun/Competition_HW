"""Independent frontend map geometry and request/source regression checks."""
from pathlib import Path
import shutil
import subprocess
import unittest


class MapGeometryFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required for frontend geometry checks")
    def test_geometry_sources_and_requests(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", str(root / "tools" / "check_map_geometry.cjs")],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
