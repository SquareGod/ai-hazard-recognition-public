from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class LocalStreamSupportTests(unittest.TestCase):
    def test_media_deployment_has_a_dedicated_local_test_publisher(self) -> None:
        compose = (REPOSITORY_ROOT / "deploy" / "compose.media.yml").read_text(encoding="utf-8")
        config = (REPOSITORY_ROOT / "deploy" / "mediamtx" / "mediamtx.yml").read_text(encoding="utf-8")
        example_environment = (REPOSITORY_ROOT / "deploy" / "env" / "media.example.env").read_text(
            encoding="utf-8"
        )

        self.assertIn("MEDIAMTX_PUBLISH_USER", compose)
        self.assertIn("MEDIAMTX_PUBLISH_PASSWORD", compose)
        self.assertIn("disabled-publish-user", config)
        self.assertIn("action: publish", config)
        self.assertIn("MEDIAMTX_PUBLISH_USER=", example_environment)
        self.assertIn("MEDIAMTX_PUBLISH_PASSWORD=", example_environment)

    def test_local_test_stream_script_exists(self) -> None:
        script = REPOSITORY_ROOT / "deploy" / "scripts" / "start-local-test-stream.ps1"
        self.assertTrue(script.is_file())
        contents = script.read_text(encoding="utf-8")
        self.assertIn("MEDIAMTX_PUBLISH_USER", contents)
        self.assertIn("testsrc2", contents)


if __name__ == "__main__":
    unittest.main()
