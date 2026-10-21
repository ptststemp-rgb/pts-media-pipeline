import importlib.util
import unittest
from unittest.mock import patch
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "internal/pipeline/assets/pts_pipeline.py"
if not PATH.exists():
    PATH = Path(__file__).resolve().parents[1] / "scripts/pts_pipeline.py"
spec = importlib.util.spec_from_file_location("pts_encoder", PATH)
encoder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(encoder)


class StorageEncodeTests(unittest.TestCase):
    def test_does_not_upscale(self):
        self.assertEqual(encoder.bounded_dimensions(640, 360, 720), (640, 360))
        self.assertEqual(encoder.bounded_dimensions(1248, 520, 720), (1248, 520))

    def test_bounds_width_and_height(self):
        self.assertEqual(encoder.bounded_dimensions(1920, 1080, 720), (1280, 720))
        self.assertEqual(encoder.bounded_dimensions(3840, 1600, 720), (1280, 532))
        self.assertEqual(encoder.bounded_dimensions(1920, 1080, 540), (960, 540))

    def test_copies_low_bitrate_aac(self):
        self.assertEqual(encoder.audio_options([
            {"codec_type": "audio", "codec_name": "aac", "channels": 2, "bit_rate": "64000"}
        ]), ["-c:a:0", "copy"])

    def test_does_not_trust_missing_aac_bitrate(self):
        self.assertEqual(encoder.audio_options([
            {"codec_type": "audio", "codec_name": "aac", "channels": 6, "tags": {"BPS": "640000"}}
        ]), ["-c:a:0", "copy"])

    def test_retains_all_languages_and_surround(self):
        audio = [{"codec_type": "video"}] + [
            {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "bit_rate": "192000"}
            for _ in range(4)
        ]
        options = encoder.audio_options(audio)
        self.assertEqual(len(options), 16)
        self.assertNotIn("-ac", options)
        for i in range(4):
            self.assertEqual(options[i * 4:(i + 1) * 4], ["-c:a:%d" % i, "aac", "-b:a:%d" % i, "256000"])

    def test_codec_conversion_uses_quality_target_not_source_codec_bitrate(self):
        options = encoder.audio_options([
            {"codec_type": "audio", "codec_name": "mp3", "channels": 2, "bit_rate": "80000"}
        ])
        self.assertEqual(options[-1], "128000")

    def test_all_channel_quality_targets(self):
        for channels, bitrate in ((1, 96000), (2, 128000), (6, 256000), (8, 384000)):
            with self.subTest(channels=channels):
                options = encoder.audio_options([{"codec_type": "audio", "codec_name": "flac", "channels": channels}])
                self.assertEqual(options[-1], str(bitrate))

    def test_single_resolution_profile(self):
        self.assertEqual(encoder.OUTPUT_HEIGHT, 720)
        self.assertEqual(encoder.bounded_dimensions(1920, 1080, encoder.OUTPUT_HEIGHT), (1280, 720))

    def test_eight_bounded_upload_connections(self):
        self.assertEqual(encoder.UPLOAD_WORKERS, 8)
        self.assertEqual(encoder.CHUNK, 8 * 1024 * 1024)

    def test_runner_time_budget_is_below_github_limit(self):
        self.assertLess(encoder.RUNNER_SECONDS, 6 * 3600)
        with patch.object(encoder.time, "monotonic", return_value=100):
            budget = encoder.RunnerBudget(seconds=10)
        with patch.object(encoder.time, "monotonic", return_value=111):
            with self.assertRaises(encoder.RunnerLimitError):
                budget.check()

    def test_runner_disk_guard(self):
        with patch.object(encoder.shutil, "disk_usage", return_value=type("Disk", (), {"free": 1000})()):
            with self.assertRaises(encoder.RunnerLimitError):
                encoder.RunnerBudget().check()

    def test_storage_encoder_is_slow_and_uses_longer_gops(self):
        options = encoder.video_options(960, 540, 29)
        self.assertIn("slow", options)
        self.assertIn("29", options)
        self.assertIn("log-level=error:keyint=96:min-keyint=1", options)


if __name__ == "__main__":
    unittest.main()
