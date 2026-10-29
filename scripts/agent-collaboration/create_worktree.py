#!/usr/bin/env python3
"""Create an agent branch and worktree from an explicitly fetched GitHub main."""
import argparse
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

URL = re.compile(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?\Z')
TASK = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--remote-url', required=True)
    parser.add_argument('--agent', required=True, choices=['codex', 'deepseek'])
    parser.add_argument('--task', required=True)
    parser.add_argument('--worktree', required=True, type=Path)
    args = parser.parse_args()
    if not URL.fullmatch(args.remote_url) or '..' in args.remote_url:
        parser.error('remote URL must be a plain HTTPS GitHub repository URL')
    if not TASK.fullmatch(args.task) or args.task == 'main':
        parser.error('task must be a short lowercase name, excluding main')
    repo = args.repo.resolve(strict=True)
    target = args.worktree.absolute()
    if os.path.lexists(target):
        parser.error('worktree destination already exists')
    if not target.parent.is_dir():
        parser.error('worktree parent must already exist')
    target = target.parent.resolve() / target.name
    branch = f'agent/{args.agent}/{args.task}'
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'

    def git(*arguments, check=True):
        return subprocess.run(['git', '-C', str(repo), '-c', 'core.hooksPath=/dev/null',
                               *arguments], env=env, check=check, text=True,
                              capture_output=True)

    root = Path(git('rev-parse', '--show-toplevel').stdout.strip()).resolve()
    if repo != root:
        parser.error('--repo must identify the root of an existing worktree')
    for line in git('worktree', 'list', '--porcelain').stdout.splitlines():
        if line.startswith('worktree '):
            existing = Path(line[9:]).resolve()
            if target == existing or existing in target.parents:
                parser.error('destination cannot be inside an existing worktree')
    if git('show-ref', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0:
        parser.error('agent branch already exists')
    ref = f'refs/agent-collaboration/fetch/{uuid.uuid4().hex}'
    try:
        git('fetch', '--no-tags', '--no-write-fetch-head', args.remote_url,
            f'+refs/heads/main:{ref}')
        git('worktree', 'add', '--no-track', '-b', branch, str(target), ref)
    finally:
        git('update-ref', '-d', ref, check=False)
    print(f'Created {branch} at {target}')
    print('Review task instructions and changes before any push or pull request.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f'Failed: {exc}', file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        sys.exit(1)
