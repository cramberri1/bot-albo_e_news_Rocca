import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('CHAT_IDS', '1')
os.environ.pop('GITHUB_ACTIONS', None)
import bot


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


class GitPersistenceTests(unittest.TestCase):
    def test_detached_head_rejected_push_rebases_and_preserves_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, repo, peer = root / 'remote.git', root / 'repo', root / 'peer'
            remote.mkdir(); repo.mkdir()
            git(remote, 'init', '--bare')
            git(repo, 'init', '-b', 'main')
            git(repo, 'config', 'user.name', 'Test')
            git(repo, 'config', 'user.email', 'test@example.invalid')
            data = repo / 'data'; data.mkdir()
            state = data / 'seen.json'; state.write_text('{}')
            git(repo, 'add', '.'); git(repo, 'commit', '-m', 'initial')
            git(repo, 'remote', 'add', 'origin', str(remote))
            git(repo, 'push', 'origin', 'HEAD:main')
            git(root, 'clone', '--branch', 'main', str(remote), str(peer))
            git(peer, 'config', 'user.name', 'Peer')
            git(peer, 'config', 'user.email', 'peer@example.invalid')
            (peer / 'remote-note').write_text('keep this')
            git(peer, 'add', '.'); git(peer, 'commit', '-m', 'remote update')
            git(peer, 'push', 'origin', 'HEAD:main')
            git(repo, 'checkout', '--detach')
            state.write_text('{"preserved": true}')
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'STATE_GIT_BRANCH': 'main'}), patch.object(bot, 'DATA_DIR', data):
                self.assertTrue(bot.git_commit_and_push([str(state)]))
            self.assertEqual(git(repo, 'rev-parse', 'HEAD'), git(remote, 'rev-parse', 'main'))
            self.assertEqual((repo / 'remote-note').read_text(), 'keep this')
            self.assertIn('preserved', state.read_text())
