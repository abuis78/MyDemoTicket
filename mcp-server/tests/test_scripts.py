"""install.sh (dry run only, never registers anything) and run.sh (with a fake keychain tool)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import MCP_DIR, CaptureServer, McpProcess, clean_env

INSTALL = os.path.join(MCP_DIR, "install.sh")
RUN = os.path.join(MCP_DIR, "run.sh")
SYSTEM_PATH = "/usr/bin:/bin"


def write_script(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix="mcp-bin-")
        self.calls = os.path.join(self.bin, "claude-calls.txt")
        # A fake claude that records any call: a dry run must never invoke it.
        write_script(self.bin, "claude", 'echo "$@" >> "%s"\n' % self.calls)

    def tearDown(self):
        shutil.rmtree(self.bin, ignore_errors=True)

    def install(self, path, *args):
        env = clean_env(PATH=path)
        return subprocess.run(["/bin/sh", INSTALL] + list(args), env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=30)

    def test_dry_run_with_claude(self):
        out = self.install(self.bin + ":" + SYSTEM_PATH, "--dry-run")
        text = out.stdout.decode()
        self.assertEqual(out.returncode, 0, text)
        self.assertIn("[dry-run] claude mcp add --scope user mydemoticket -- %s" % RUN, text)
        self.assertFalse(os.path.exists(self.calls), "dry run invoked claude")

    def test_dry_run_uninstall_with_claude(self):
        out = self.install(self.bin + ":" + SYSTEM_PATH, "--dry-run", "--uninstall", "--scope", "project",
                           "--name", "mdt")
        text = out.stdout.decode()
        self.assertEqual(out.returncode, 0, text)
        self.assertIn("[dry-run] claude mcp remove --scope project mdt", text)
        self.assertFalse(os.path.exists(self.calls), "dry run invoked claude")

    def test_dry_run_without_claude(self):
        if shutil.which("claude", path=SYSTEM_PATH):
            self.skipTest("claude is installed in %s" % SYSTEM_PATH)
        for args in (["--dry-run"], ["--dry-run", "--uninstall"]):
            with self.subTest(args=args):
                out = self.install(SYSTEM_PATH, *args)
                text = out.stdout.decode()
                self.assertEqual(out.returncode, 0, text)
                self.assertIn("not installed", text)
                self.assertIn("claude_desktop_config.json", text)
        self.assertIn('"command": "%s"' % RUN, self.install(SYSTEM_PATH, "--dry-run").stdout.decode())

    def test_bad_option(self):
        self.assertEqual(self.install(self.bin + ":" + SYSTEM_PATH, "--scope", "galaxy").returncode, 2)


class RunScriptTests(unittest.TestCase):
    """run.sh with a fake `security` first in PATH, so the real keychain is never touched."""

    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix="mcp-bin-")
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.stub = CaptureServer(body={"username": "mcp", "role": "integration"})

    def tearDown(self):
        self.stub.stop()
        shutil.rmtree(self.bin, ignore_errors=True)

    def start(self, security_body, **env):
        write_script(self.bin, "security", security_body)
        env = clean_env(PATH=self.bin + ":" + SYSTEM_PATH, MYDEMOTICKET_URL=self.stub.url, **env)
        env.pop("PYTHONPATH", None)
        mcp = McpProcess(env, argv=["/bin/sh", RUN])
        mcp._env.pop("PYTHONPATH", None)  # run.sh must set it itself
        return mcp.start()

    def whoami(self, mcp):
        try:
            self.assertEqual(mcp.initialize()["result"]["serverInfo"]["name"], "mydemoticket")
            return mcp.call("whoami", {})
        finally:
            mcp.stop()

    def test_token_from_keychain(self):
        body = ('[ "$1 $2 $3 $4 $5 $6" = "find-generic-password -s mydemoticket -a kc-account -w" ] || exit 44\n'
                'echo keychain-test-token\n')
        result = self.whoami(self.start(body, MYDEMOTICKET_KEYCHAIN_ACCOUNT="kc-account"))
        self.assertIs(result["isError"], False, result)
        self.assertEqual(self.stub.requests[-1]["headers"]["authorization"], "Bearer keychain-test-token")

    def test_env_token_wins_over_keychain(self):
        result = self.whoami(self.start("exit 45\n", MYDEMOTICKET_TOKEN="env-test-token"))
        self.assertIs(result["isError"], False, result)
        self.assertEqual(self.stub.requests[-1]["headers"]["authorization"], "Bearer env-test-token")

    def test_missing_keychain_item_still_starts(self):
        mcp = self.start("exit 44\n")
        result = self.whoami(mcp)
        self.assertIs(result["isError"], True)
        self.assertIn("MYDEMOTICKET_TOKEN is not set", result["content"][0]["text"])
        self.assertEqual(self.stub.requests, [])


if __name__ == "__main__":
    unittest.main()
