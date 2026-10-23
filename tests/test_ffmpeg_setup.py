import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / 'internal/pipeline/assets/pts-download.yml'
if not WORKFLOW.exists():
    WORKFLOW = ROOT / '.github/workflows/pts-download.yml'


class FFmpegSetupTests(unittest.TestCase):
    def run_setup(self, mode):
        text = WORKFLOW.read_text()
        step = text.split('      - name: Ensure ffmpeg with HEVC support', 1)[1]
        body = step.split('        run: |\n', 1)[1].split('\n      - name:', 1)[0]
        script = '\n'.join(line[10:] for line in body.splitlines() if line.strip())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = {
                'sudo': '#!/bin/bash\nexec "$@"\n',
                'sleep': '#!/bin/bash\nexit 0\n',
                'ffprobe': '#!/bin/bash\necho "ffprobe test version"\n',
                'ffmpeg': '#!/bin/bash\nif [[ "$*" == *-encoders* ]]; then if [[ "$MODE" == missing_hevc ]]; then echo libx264; else echo libx265; fi; else echo "ffmpeg test version"; fi\n',
                'apt-get': '''#!/bin/bash
echo "$*" >> "$FIXTURE/apt-calls"
if [[ "$*" == *update ]]; then
  if [[ "$MODE" == retry_update && ! -e "$FIXTURE/update-failed" ]]; then touch "$FIXTURE/update-failed"; exit 1; fi
  exit 0
fi
if [[ "$MODE" == fail_install ]]; then exit 1; fi
touch "$FIXTURE/installed"
''',
            }
            for name, contents in tools.items():
                file = root / name
                file.write_text(contents)
                file.chmod(0o755)
            if mode in ('already_installed', 'missing_hevc'):
                (root / 'installed').touch()
            prefix = '''command() {
if [[ "$1" == -v && ( "$2" == ffmpeg || "$2" == ffprobe ) && ! -e "$FIXTURE/installed" ]]; then return 1; fi
builtin command "$@"
}
'''
            env = {**os.environ, 'PATH': str(root) + ':' + os.environ['PATH'], 'FIXTURE': str(root), 'RUNNER_TEMP': str(root), 'MODE': mode}
            result = subprocess.run(['bash', '-c', prefix + script], env=env, capture_output=True, text=True, timeout=10)
            calls = (root / 'apt-calls').read_text().splitlines() if (root / 'apt-calls').exists() else []
            return result, calls

    def test_installed_ffmpeg_skips_package_manager(self):
        result, calls = self.run_setup('already_installed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])
        self.assertIn('HEVC support verified', result.stdout)

    def test_transient_update_failure_retries_successfully(self):
        result, calls = self.run_setup('retry_update')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([call for call in calls if call.endswith('update')]), 2)
        self.assertIn('attempt 2/2', result.stdout)
        self.assertTrue(all('Acquire::http::Timeout=15' in call for call in calls))

    def test_repeated_install_failure_exits_before_encoding(self):
        result, calls = self.run_setup('fail_install')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([call for call in calls if 'install -y' in call]), 2)
        self.assertIn('two bounded installation attempts', result.stdout)

    def test_missing_hevc_is_reported_explicitly(self):
        result, _ = self.run_setup('missing_hevc')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not include the libx265', result.stdout)


if __name__ == '__main__':
    unittest.main()
