from __future__ import annotations

import struct
import unittest
from unittest.mock import patch

from app.audio import player as audio_player


class FakeSoundDevice:
    def query_devices(self):
        return [{"name": "Loa thử", "max_output_channels": 2, "default_samplerate": 44100}]


class ResamplerTest(unittest.TestCase):
    def test_empty_identity_single_sample_va_length(self) -> None:
        self.assertEqual(audio_player._resample_pcm(b"", 24000, 44100), b"")
        pcm = struct.pack("<3h", -1000, 0, 1000)
        self.assertIs(audio_player._resample_pcm(pcm, 24000, 24000), pcm)
        single = struct.pack("<h", 123)
        self.assertEqual(
            audio_player._resample_pcm(single, 1, 3), single * 3
        )
        output = audio_player._resample_pcm(pcm, 24000, 48000)
        self.assertEqual(len(output), len(pcm) * 2)
        self.assertEqual(struct.unpack("<h", output[:2])[0], -1000)
        self.assertEqual(struct.unpack("<h", output[-2:])[0], 1000)

    def test_byte_le_bi_bo_qua(self) -> None:
        self.assertEqual(audio_player._resample_pcm(b"\x01", 24000, 44100), b"")


class PlayerSelectionTest(unittest.TestCase):
    def test_auto_uu_tien_sounddevice(self) -> None:
        with patch.object(audio_player, "_import_sounddevice", return_value=FakeSoundDevice()):
            selected = audio_player.create_player("auto", "Loa")
        self.assertIsInstance(selected, audio_player.SoundDevicePlayer)

    def test_thieu_sounddevice_fallback_ffplay_roi_none(self) -> None:
        with (
            patch.object(audio_player, "_import_sounddevice", return_value=None),
            patch.object(audio_player.shutil, "which", return_value="C:/ffplay.exe"),
        ):
            selected = audio_player.create_player("auto")
        self.assertIsInstance(selected, audio_player.FfplayPlayer)

        with (
            patch.object(audio_player, "_import_sounddevice", return_value=None),
            patch.object(audio_player.shutil, "which", return_value=None),
        ):
            self.assertIsNone(audio_player.create_player("auto"))
            self.assertIsNone(audio_player.create_player("none"))


if __name__ == "__main__":
    unittest.main()
