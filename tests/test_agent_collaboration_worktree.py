"""Real Git fixtures for independent agent worktree creation."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/agent-collaboration/create_worktree.py'


def git(*args, cwd):
    return subprocess.run(['git', *map(str, args)], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.repo = self.root / 'source repo'
        git('init', '--bare', self.remote, cwd=self.root)
        git('init', '-b', 'main', self.repo, cwd=self.root)
        git('config', 'user.name', 'Test', cwd=self.repo)
        git('config', 'user.email', 'test@example.invalid', cwd=self.repo)
        (self.repo / 'tracked.txt').write_text('base\n')
        git('add', 'tracked.txt', cwd=self.repo)
        git('commit', '-m', 'base', cwd=self.repo)
        git('push', self.remote, 'main', cwd=self.repo)
        self.url = 'https://github.com/example/test.git'
        git('config', f'url.{self.remote.as_uri()}.insteadOf', self.url, cwd=self.repo)
        (self.repo / 'tracked.txt').write_text('unsaved change\n')
        self.target = self.root / 'agent workspace'

    def invoke(self, *extra, url=None, task='sample', target=None):
        return subprocess.run([sys.executable, str(SCRIPT), '--repo', str(self.repo),
                               '--remote-url', url or self.url, '--agent', 'deepseek',
                               '--task', task, '--worktree', str(target or self.target), *extra],
                              text=True, capture_output=True)

    def test_creates_new_branch_at_fetched_main_preserving_source(self):
        other = self.root / 'remote update'
        git('clone', '--branch', 'main', self.remote, other, cwd=self.root)
        git('config', 'user.name', 'Test', cwd=other)
        git('config', 'user.email', 'test@example.invalid', cwd=other)
        (other / 'tracked.txt').write_text('remote update\n')
        git('commit', '-am', 'remote update', cwd=other)
        git('push', 'origin', 'main', cwd=other)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git('branch', '--show-current', cwd=self.target), 'agent/deepseek/sample')
        self.assertEqual((self.target / 'tracked.txt').read_text(), 'remote update\n')
        self.assertEqual((self.repo / 'tracked.txt').read_text(), 'unsaved change\n')
        self.assertEqual(git('branch', '--show-current', cwd=self.repo), 'main')

    def test_existing_path_is_never_overwritten(self):
        self.target.mkdir()
        (self.target / 'keep').write_text('keep')
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual((self.target / 'keep').read_text(), 'keep')
        self.assertEqual(git('branch', '--list', 'agent/deepseek/sample', cwd=self.repo), '')

    def test_existing_branch_is_never_reused(self):
        git('branch', 'agent/deepseek/sample', cwd=self.repo)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertFalse(self.target.exists())

    def test_failed_fetch_creates_no_branch_or_target(self):
        missing_url = 'https://github.com/example/missing.git'
        git('config', f'url.{(self.root / "missing.git").as_uri()}.insteadOf', missing_url, cwd=self.repo)
        result = self.invoke(url=missing_url)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())
        self.assertEqual(git('branch', '--list', 'agent/deepseek/sample', cwd=self.repo), '')

    def test_rejects_unsafe_url_and_target_inside_repo(self):
        self.assertNotEqual(self.invoke(url=str(self.remote)).returncode, 0)
        self.assertNotEqual(self.invoke(target=self.repo / 'nested').returncode, 0)
        self.assertFalse((self.repo / 'nested').exists())

    def test_rejects_invalid_task_and_agent(self):
        self.assertNotEqual(self.invoke(task='main').returncode, 0)
        self.assertNotEqual(self.invoke(task='../bad').returncode, 0)
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
