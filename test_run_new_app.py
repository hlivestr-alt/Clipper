from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "PowerShell launcher tests require Windows")
class RunNewAppSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = Path(__file__).resolve().parent / "run_new_app.ps1"

    def _run_frontend_only(self, token: str | None) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        if token is None:
            environment.pop("CLIPPER_CONTROL_TOKEN", None)
        else:
            environment["CLIPPER_CONTROL_TOKEN"] = token
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script),
                "-FrontendOnly",
                "-PnpmExe",
                "Write-Output",
            ],
            cwd=self.script.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_frontend_only_requires_environment_token(self) -> None:
        result = self._run_frontend_only(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CLIPPER_CONTROL_TOKEN", result.stderr)

    def test_frontend_only_uses_environment_without_echoing_token(self) -> None:
        token = "launcher-test-secret"
        result = self._run_frontend_only(token)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["dev", "--host", "127.0.0.1", "--port", "5173"])


if __name__ == "__main__":
    unittest.main()
