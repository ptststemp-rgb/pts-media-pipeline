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
        self.assertEqual(encoder.bounded_dimensions(640, 360, 540), (640, 360))
        self.assertEqual(encoder.bounded_dimensions(854, 480, 540), (854, 480))
        self.assertEqual(encoder.bounded_dimensions(960, 540, 540), (960, 540))

    def test_bounds_width_and_height(self):
        self.assertEqual(encoder.bounded_dimensions(1920, 1080, 540), (960, 540))
        self.assertEqual(encoder.bounded_dimensions(1280, 720, 540), (960, 540))
        self.assertEqual(encoder.bounded_dimensions(3840, 1600, 540), (960, 400))

    def test_video_profile_is_fixed_hevc_crf23_medium_hvc1(self):
        options = encoder.video_options(960, 540, 23)
        self.assertIn("libx265", options)
        self.assertIn("medium", options)
        self.assertIn("23", options)
        self.assertIn("yuv420p", options)
        self.assertIn("hvc1", options)
        i = options.index("-force_key_frames")
        self.assertEqual(options[i + 1], "expr:gte(t,n_forced*2)")
        self.assertNotIn("-reserve_index_space", options)

    def test_copies_compatible_stereo_aac(self):
        for bitrate in ("96000", "128000"):
            with self.subTest(bitrate=bitrate):
                stream = {"codec_type": "audio", "codec_name": "aac", "channels": 2,
                          "sample_rate": "48000"}
                if bitrate:
                    stream["bit_rate"] = bitrate
                self.assertEqual(encoder.audio_options([stream]), ["-c:a:0", "copy"])

    def test_reencodes_high_bitrate_aac_down_not_up(self):
        options = encoder.audio_options([
            {"codec_type": "audio", "codec_name": "aac", "channels": 2,
             "bit_rate": "256000", "sample_rate": "48000"}
        ])
        self.assertEqual(options, ["-c:a:0", "aac", "-b:a:0", "128000", "-ac:a:0", "2", "-ar:a:0", "48000"])

    def test_transcodes_surround_and_other_codecs_to_aac_stereo_128k(self):
        for codec, channels in (("eac3", 6), ("ac3", 6), ("dts", 8), ("eac3", 2)):
            with self.subTest(codec=codec, channels=channels):
                options = encoder.audio_options([
                    {"codec_type": "audio", "codec_name": codec, "channels": channels,
                     "sample_rate": "48000"}
                ])
                self.assertEqual(options, ["-c:a:0", "aac", "-b:a:0", "128000",
                                           "-ac:a:0", "2", "-ar:a:0", "48000"])

    def test_select_audio_prefers_stereo_and_drops_duplicate_surround(self):
        streams = [
            {"codec_type": "video"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
             "bit_rate": "128000", "tags": {"language": "eng"}},
            {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
             "bit_rate": "640000", "tags": {"language": "eng"}},
            {"index": 3, "codec_type": "audio", "codec_name": "aac", "channels": 2,
             "bit_rate": "128000", "tags": {"language": "tel"}},
            {"index": 4, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
             "bit_rate": "640000", "tags": {"language": "tel"}},
            {"index": 5, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
             "bit_rate": "640000", "tags": {"language": "hin"}},
        ]
        selected = encoder.select_audio(streams)
        self.assertEqual([s["index"] for s in selected], [1, 3, 5])

    def test_select_audio_keeps_surround_only_language(self):
        streams = [
            {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
             "bit_rate": "384000", "tags": {"language": "hin"}},
            {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 8,
             "bit_rate": "640000", "tags": {"language": "hin"}},
        ]
        self.assertEqual([s["index"] for s in encoder.select_audio(streams)], [2])

    def test_select_audio_keeps_single_track(self):
        streams = [{"index": 1, "codec_type": "audio", "channels": 2}]
        self.assertEqual(encoder.select_audio(streams), streams)

    def test_audio_metadata_preserves_language_and_title(self):
        streams = [
            {"codec_type": "audio", "tags": {"language": "eng", "title": "English"}},
            {"codec_type": "audio", "tags": {"language": "hin"}},
        ]
        self.assertEqual(encoder.audio_metadata(streams), [
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=English",
            "-metadata:s:a:1", "language=hin",
        ])

    def test_single_resolution_profile(self):
        self.assertEqual(encoder.OUTPUT_HEIGHT, 540)
        self.assertEqual(encoder.AUDIO_BITRATE, 128000)

    def test_unknown_bitrate_aac_does_not_bypass_128k_target(self):
        options = encoder.audio_options([{"codec_name": "aac", "channels": 2, "sample_rate": "48000"}])
        self.assertIn("128000", options)
        self.assertNotIn("copy", options)

    def test_previous_160k_audio_is_reduced_to_128k(self):
        options = encoder.audio_options([{"codec_name": "aac", "channels": 2, "sample_rate": "48000", "bit_rate": "160000"}])
        self.assertIn("128000", options)
        self.assertNotIn("copy", options)

    def test_unknown_audio_tracks_are_not_discarded(self):
        tracks = [{"index": i, "codec_type": "audio", "codec_name": "aac", "channels": 2} for i in (1, 2)]
        self.assertEqual([s["index"] for s in encoder.select_audio(tracks)], [1, 2])
        tracks[0]["tags"] = {"language": "und"}
        tracks[1]["tags"] = {"language": "unknown"}
        self.assertEqual(len(encoder.select_audio(tracks)), 2)

    def test_compatible_44100_aac_is_copied(self):
        options = encoder.audio_options([{"codec_name": "aac", "channels": 2, "bit_rate": "128000", "sample_rate": "44100"}])
        self.assertEqual(options, ["-c:a:0", "copy"])

    def test_workflow_never_exports_source_urls(self):
        workflow = PATH.with_name("pts-download.yml")
        if not workflow.exists():
            workflow = PATH.parent.parent / ".github/workflows/pts-download.yml"
        text = workflow.read_text()
        self.assertNotIn("SOURCE_URL:", text)
        self.assertNotIn("${{ inputs.url }}", text)

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


if __name__ == "__main__":
    unittest.main()
