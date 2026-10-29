"""Exercise the release script against fake SSH and Docker, never a live host."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy_vps.sh"
OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64
SITE = "crypto-project:jindan-v1-20261029"
RECOVERY = "crypto-project:rollback-performance-test"

SSH = '''#!/usr/bin/env python3
import os, sys
with open(os.environ['TEST_CALLS'], 'a') as f:
    f.write('ssh ' + ' '.join(sys.argv[1:]) + '\\n')
if os.environ.get('TEST_SSH_FAILURE'): sys.exit(255)
if os.environ.get('TEST_SSH_OUTPUT'):
    print(os.environ['TEST_SSH_OUTPUT'])
    sys.exit(0)
args = sys.argv[1:]
while args and args[0] == '-o': args = args[2:]
args = args[1:]  # SSH host
os.execvp(args[0], args)
'''
SUDO = '''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args and args[0] == '-n': args = args[1:]
os.execvp(args[0], args)
'''
DOCKER = '''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ['TEST_CALLS'], 'a') as f:
    f.write('docker ' + json.dumps(a) + '\\n')
if a[:1] == ['inspect']:
    with open(os.environ['TEST_CALLS']) as f:
        inspections = sum(line.startswith('docker ["inspect", ') for line in f)
    current = os.environ['TEST_CURRENT']
    if os.environ.get('TEST_CHANGE_DURING') and inspections > 1: current = os.environ['TEST_CHANGED']
    print('|'.join([current, os.environ['TEST_SITE'], 'crypto-project', 'crypto-project',
                    '/opt/crypto-project', '/opt/crypto-project/docker-compose.yml,/opt/crypto-project/deploy/jindan-v1/web-override.yml']))
elif a[:2] == ['image', 'inspect']:
    key = a[2]
    values = {os.environ['TEST_NEW']: os.environ['TEST_NEW'], os.environ['TEST_SITE']: os.environ['TEST_TARGET'],
              os.environ['TEST_RECOVERY']: os.environ['TEST_BACKUP']}
    if key not in values or values[key] == 'missing': sys.exit(1)
    print(values[key])
elif a[:2] == ['image', 'tag']:
    pass
elif a[:1] == ['compose'] and 'config' in a:
    print(os.environ['TEST_COMPOSE_IMAGE'])
elif a[:1] == ['compose'] and 'up' in a:
    pass
else:
    sys.exit(90)
'''
RSYNC = '''#!/usr/bin/env python3
import os, sys
with open(os.environ['TEST_CALLS'], 'a') as f: f.write('rsync ' + ' '.join(sys.argv[1:]) + '\\n')
sys.exit(72)
'''


class OverlayDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name, body in (("ssh", SSH), ("sudo", SUDO), ("docker", DOCKER), ("rsync", RSYNC)):
            target = self.root / name
            target.write_text(body)
            target.chmod(0o755)
        self.calls = self.root / "calls"
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ.get("PATH", ""),
                        TEST_CALLS=str(self.calls), TEST_CURRENT=OLD, TEST_NEW=NEW,
                        TEST_SITE=SITE, TEST_RECOVERY=RECOVERY, TEST_TARGET=OLD, TEST_BACKUP=OLD, TEST_CHANGED=OTHER,
                        TEST_COMPOSE_IMAGE=SITE, REMOTE_HOST="crypto-vps", SERVICE="crypto-project",
                        VPS_OVERLAY_RELEASE="yes", VPS_OVERLAY_CANDIDATE_ID=NEW,
                        VPS_OVERLAY_ACCEPTED_IMAGE_ID=NEW,
                        VPS_OVERLAY_EXPECTED_IMAGE_ID=OLD, VPS_OVERLAY_SITE_TAG=SITE,
                        VPS_OVERLAY_RECOVERY_TAG=RECOVERY)

    def run_script(self, **updates):
        result = subprocess.run(["bash", str(SCRIPT)], cwd=self.root, env={**self.env, **updates},
                                text=True, capture_output=True, timeout=10)
        calls = self.calls.read_text() if self.calls.exists() else ""
        return result, calls

    def test_overlay_uses_existing_compose_and_only_restarts_website(self):
        result, calls = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("rsync ", calls)
        self.assertIn('"image", "tag", "' + NEW + '", "' + SITE + '"', calls)
        self.assertIn('"-f", "/opt/crypto-project/docker-compose.yml"', calls)
        self.assertIn('"-f", "/opt/crypto-project/deploy/jindan-v1/web-override.yml"', calls)
        self.assertIn('"up", "-d", "--no-build", "--no-deps", "crypto-project"', calls)

    def test_drift_or_missing_preconditions_never_tag_or_restart(self):
        for updates in ({"TEST_CURRENT": OTHER}, {"TEST_TARGET": OTHER}, {"TEST_BACKUP": OTHER},
                        {"TEST_BACKUP": "missing"}, {"TEST_COMPOSE_IMAGE": "other:tag"},
                        {"TEST_CHANGE_DURING": "yes"},
                        {"VPS_OVERLAY_CANDIDATE_ID": OTHER}):
            with self.subTest(updates=updates):
                self.calls.unlink(missing_ok=True)
                result, calls = self.run_script(**updates)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('"image", "tag"', calls)
                self.assertNotIn('"up",', calls)
                self.assertNotIn("rsync ", calls)

    def test_bad_values_and_other_services_stop_before_ssh(self):
        for updates in ({"SERVICE": "crypto-project okx-strategy-bot"},
                        {"REMOTE_HOST": "other-host"}, {"VPS_OVERLAY_RELEASE": "no"},
                        {"VPS_OVERLAY_SITE_TAG": "bad;touch /tmp/unsafe"},
                        {"VPS_OVERLAY_RECOVERY_TAG": SITE},
                        {"VPS_OVERLAY_ACCEPTED_IMAGE_ID": OTHER},
                        {"VPS_OVERLAY_EXPECTED_IMAGE_ID": "sha256:bad"},
                        {"VPS_OVERLAY_CANDIDATE_ID": OLD}):
            with self.subTest(updates=updates):
                self.calls.unlink(missing_ok=True)
                result, calls = self.run_script(**updates)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, "")

    def test_default_path_still_invokes_its_existing_rsync(self):
        result, calls = self.run_script(VPS_OVERLAY_RELEASE="", REMOTE_DIR=str(self.remote))
        self.assertEqual(result.returncode, 72)
        self.assertIn("rsync ", calls)
        self.assertIn("--delete", calls)
        self.assertIn("ssh ", calls)
        self.assertLess(calls.index("ssh "), calls.index("rsync "))

    def test_default_path_blocks_existing_or_dangling_override_before_sync(self):
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                self.calls.unlink(missing_ok=True)
                target = self.remote / "deploy" / "jindan-v1" / "web-override.yml"
                target.parent.mkdir(parents=True, exist_ok=True)
                if dangling:
                    target.symlink_to("missing-override")
                else:
                    target.write_text("services: {}\n")
                try:
                    result, calls = self.run_script(VPS_OVERLAY_RELEASE="", REMOTE_DIR=str(self.remote))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("overlay", result.stderr.lower())
                    self.assertIn("ssh ", calls)
                    self.assertNotIn("rsync ", calls)
                    self.assertNotIn("docker ", calls)
                finally:
                    target.unlink()

    def test_default_path_fails_closed_when_override_status_is_unknown(self):
        for updates in ({"TEST_SSH_FAILURE": "yes"}, {"TEST_SSH_OUTPUT": "unexpected"},
                        {"REMOTE_DIR": str(self.remote / "missing")}):
            with self.subTest(updates=updates):
                self.calls.unlink(missing_ok=True)
                result, calls = self.run_script(**{"VPS_OVERLAY_RELEASE": "", "REMOTE_DIR": str(self.remote), **updates})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ssh ", calls)
                self.assertNotIn("rsync ", calls)
                self.assertNotIn("docker ", calls)

    def test_default_path_rejects_unsafe_remote_paths_before_ssh(self):
        for remote in ("/opt/../other", "/opt/with space", "/tmp/$(echo unsafe)"):
            with self.subTest(remote=remote):
                self.calls.unlink(missing_ok=True)
                result, calls = self.run_script(VPS_OVERLAY_RELEASE="", REMOTE_DIR=remote)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, "")


if __name__ == "__main__":
    unittest.main()
