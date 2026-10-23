import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import errno

PATH = Path(__file__).resolve().parents[1] / 'internal/hls/assets/hls.py'
if not PATH.exists():
    PATH = Path(__file__).resolve().parents[1] / 'scripts/pts_hls_helper.py'
spec = importlib.util.spec_from_file_location('hls_safety', PATH)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class HLSSafetyTests(unittest.TestCase):
    def test_mergerfs_fallback_publishes_metadata_last(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.fixture(directory)
            destination = Path(directory) / 'published.hls'
            moves = []
            rename = helper.os.rename
            def track(src, dst):
                moves.append(Path(src).name)
                rename(src, dst)
            with patch.object(helper, 'rename_no_replace', side_effect=OSError(errno.EINVAL, 'Unsupported flag')), patch.object(helper.os, 'rename', side_effect=track):
                helper.rename_new(source, destination)
            self.assertEqual(moves[-1], 'pts-hls.json')
            self.assertFalse(source.exists())
            helper.verify(destination)

    def test_mergerfs_fallback_never_overwrites_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.fixture(directory)
            destination = Path(directory) / 'existing.hls'
            destination.mkdir()
            with patch.object(helper, 'rename_no_replace', side_effect=OSError(errno.EINVAL, 'Unsupported flag')):
                with self.assertRaises(FileExistsError):
                    helper.rename_new(source, destination)
            self.assertTrue(source.exists())
            self.assertEqual(list(destination.iterdir()), [])

    def test_mergerfs_partial_publish_is_not_visible_to_scanner(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.fixture(directory)
            source.rename(Path(directory) / '.incoming')
            source = Path(directory) / '.incoming'
            destination = Path(directory) / 'published.hls'
            rename = helper.os.rename
            scans = []
            def observe(src, dst):
                scans.append(helper.scan([directory])['items'])
                rename(src, dst)
            with patch.object(helper, 'rename_no_replace', side_effect=OSError(errno.EINVAL, 'Unsupported flag')), patch.object(helper.os, 'rename', side_effect=observe):
                helper.rename_new(source, destination)
            self.assertTrue(all(items == [] for items in scans))
            self.assertEqual(len(helper.scan([directory])['items']), 1)

    def fixture(self, parent):
        root = Path(parent) / 'Fixture.hls'
        (root / 'v0').mkdir(parents=True)
        (root / 'master.m3u8').write_text('#EXTM3U\nv0/index.m3u8\n')
        (root / 'v0/index.m3u8').write_text('#EXTM3U\n#EXT-X-MAP:URI="init_0.mp4"\nseg_00000.m4s\n#EXT-X-ENDLIST\n')
        (root / 'v0/init_0.mp4').write_bytes(b'initialization')
        (root / 'v0/seg_00000.m4s').write_bytes(b'encoded packets')
        files = {str(p.relative_to(root)): {'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in root.rglob('*') if p.is_file()}
        doc = {'version': 1, 'size': sum(p['size'] for p in files.values()), 'duration': 20, 'video_codec': 'hevc', 'width': 960, 'height': 540, 'audio': [], 'files': files}
        (root / 'pts-hls.json').write_text(json.dumps(doc))
        return root

    def test_verified_archive_extracts_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.fixture(directory)
            archive = Path(directory) / 'fixture.tar'
            with tarfile.open(archive, 'w') as output:
                for file in sorted(source.rglob('*')):
                    output.add(file, arcname=str(file.relative_to(source)), recursive=False)
            doc = helper.inspect_archive(archive)
            destination = Path(directory) / 'copy.hls'
            self.assertEqual(helper.extract(archive, destination)['files'], doc['files'])
            self.assertEqual(helper.verify(destination)['files'], doc['files'])

    def test_corrupted_media_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'v0/seg_00000.m4s').write_bytes(b'wrong contents!')
            with self.assertRaises(ValueError):
                helper.verify(root)

    def test_unsafe_tar_entry_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'unsafe.tar'
            with tarfile.open(archive, 'w') as output:
                entry = tarfile.TarInfo('../outside');entry.size = 4
                output.addfile(entry, io.BytesIO(b'test'))
            with self.assertRaises(ValueError):
                helper.inspect_archive(archive)
            with self.assertRaises(ValueError):
                helper.extract(archive, Path(directory) / 'target')

    def test_playlist_cannot_reference_external_or_parent_path(self):
        for reference in ('https://example.invalid/file', '../outside', '/etc/passwd'):
            with tempfile.TemporaryDirectory() as directory:
                root = self.fixture(directory)
                master = root / 'master.m3u8';master.write_text('#EXTM3U\n' + reference + '\n')
                doc = json.loads((root / 'pts-hls.json').read_text())
                doc['files']['master.m3u8'] = {'size': master.stat().st_size, 'sha256': helper.digest(master)}
                doc['size'] = sum(item['size'] for item in doc['files'].values())
                (root / 'pts-hls.json').write_text(json.dumps(doc))
                with self.assertRaises(ValueError):
                    helper.verify(root)

    def test_scan_does_not_index_initialization_mp4_as_movies(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.fixture(directory)
            result = helper.scan([directory])['items']
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['path'], str(source / 'master.m3u8'))
            self.assertEqual(result[0]['hls']['video_codec'], 'hevc')

    def test_expired_download_removed_but_unrelated_files_retained(self):
        old = helper.DOWNLOAD_ROOT
        try:
            with tempfile.TemporaryDirectory() as directory:
                helper.DOWNLOAD_ROOT = Path(directory)
                expired = helper.DOWNLOAD_ROOT / ('a' * 32);expired.mkdir()
                helper.save_state(expired, {'created': int(time.time()) - 14400, 'expires': int(time.time()) + 3600})
                (expired / 'movie.mp4').write_bytes(b'temporary')
                unrelated = helper.DOWNLOAD_ROOT / 'unrelated';unrelated.mkdir()
                self.assertEqual(helper.clean_downloads()['removed'], 1)
                self.assertFalse(expired.exists());self.assertTrue(unrelated.exists())
        finally:
            helper.DOWNLOAD_ROOT = old

    def test_identifiers_cannot_escape_download_root(self):
        for identifier in ('../movie', '/tmp/file', 'a' * 31, 'Z' * 32):
            with self.assertRaises(ValueError):
                helper.download_dir(identifier)


if __name__ == '__main__':
    unittest.main()
