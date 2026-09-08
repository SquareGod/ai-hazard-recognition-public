from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class RuntimeLauncherSupportTests(unittest.TestCase):
    def test_public_readme_describes_safe_environment_configuration(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(".env.example", readme)
        self.assertIn("no camera credentials", readme)
        self.assertIn("Mock", readme)

    def test_public_launcher_uses_docker_compose_without_machine_paths(self) -> None:
        script = REPOSITORY_ROOT / "scripts" / "start.ps1"

        self.assertTrue(script.is_file())
        contents = script.read_text(encoding="utf-8")
        self.assertIn("docker compose up --build -d", contents)
        self.assertNotIn("D:\\python", contents)
        self.assertNotIn("D:\\node", contents)


if __name__ == "__main__":
    unittest.main()
