from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RuntimeLauncherTests(unittest.TestCase):
    def test_normal_launcher_supervises_main_for_future_code_updates(self):
        launcher = (ROOT / "run.bat").read_text(encoding="utf-8")

        self.assertIn("python dev_reload.py", launcher)
        self.assertIsNone(re.search(r"(?m)^python main\.py\s*$", launcher))


if __name__ == "__main__":
    unittest.main()
