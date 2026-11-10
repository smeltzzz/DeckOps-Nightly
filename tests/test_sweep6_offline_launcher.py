"""Sweep 6: the offline launcher's music toggle (fake pygame, temp settings)."""
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    import launcher_plut_win as launcher
except ImportError:
    launcher = None


class _FakeMusic:
    """pygame.mixer.music: busy while playing *and* while a fadeout runs."""

    def __init__(self):
        self.busy = False
        self.fading = False
        self.plays = 0

    def get_busy(self):
        return self.busy

    def load(self, path):
        pass

    def set_volume(self, v):
        pass

    def play(self, loops=0):
        self.busy = True
        self.fading = False
        self.plays += 1

    def fadeout(self, ms):
        self.fading = True          # still busy until the fade ends

    def stop(self):
        self.busy = False
        self.fading = False

    def finish_fade(self):
        if self.fading:
            self.stop()


def _fake_pygame():
    music = _FakeMusic()
    mixer = types.SimpleNamespace(get_init=lambda: True, init=lambda: None, music=music)
    return types.SimpleNamespace(mixer=mixer), music


@unittest.skipIf(launcher is None, "PyQt5 is not installed")
class MusicToggleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        music_file = root / "music.mp3"
        music_file.write_bytes(b"x")
        self.pygame, self.music = _fake_pygame()
        for p in (patch.dict(sys.modules, {"pygame": self.pygame}),
                  patch.object(launcher, "_SETTINGS_FILE", str(root / ".lan_launcher.json")),
                  patch.object(launcher, "_DECKOPS_JSON", str(root / "deckops.json")),
                  patch.object(launcher, "MUSIC_PATH", str(music_file)),
                  patch.object(launcher, "_audio_fading", False)):
            p.start()
            self.addCleanup(p.stop)

    def _toggle(self):
        # The window's own slot, without building the window
        window = types.SimpleNamespace(_update_music_btn=lambda: None)
        launcher.LauncherWindow._toggle_music(window)

    def test_unmute_during_the_fadeout_keeps_music_playing(self):
        launcher._set_music_muted(False)
        launcher._start_audio()
        self.assertTrue(self.music.busy)
        self._toggle()                      # mute: fadeout starts
        self._toggle()                      # unmute within the fade
        self.music.finish_fade()            # the old fade would end here
        self.assertFalse(launcher._music_muted())
        self.assertTrue(self.music.busy, "music stopped while the button says MUSIC ON")
        self.assertEqual(self.music.plays, 2)

    def test_start_while_playing_does_not_restart_the_track(self):
        launcher._set_music_muted(False)
        launcher._start_audio()
        launcher._start_audio()
        self.assertEqual(self.music.plays, 1)

    def test_unmute_after_the_fade_ended_plays_again(self):
        launcher._set_music_muted(False)
        launcher._start_audio()
        self._toggle()
        self.music.finish_fade()
        self._toggle()
        self.assertTrue(self.music.busy)


if __name__ == "__main__":
    unittest.main()
