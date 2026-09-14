"""The detection matrix of spec 13, against committed dumps.

Three shapes must trigger, and two near-misses must not: a browser playing a
video (playback, no capture) and a Teams tab with no call (the title matches,
but condition 1 does not). Condition 1 and condition 2 are asserted separately,
because keeping them independent is what makes another platform configuration
rather than code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from munin.detect.base import AppRule, CallEvidence, Detector, WindowInfo, identify
from munin.detect.linux import (
    CallWatcher,
    PipewireDetector,
    default_source_name,
    hyprland_signature,
    parse_clients,
    parse_evidence,
    parse_windows,
    read_ppid,
    window_for_pid,
)

OSLO = timezone(timedelta(hours=2))


class FakeDetector(Detector):
    """A detector whose evidence and windows come from committed fixtures."""

    platform = "linux"

    def __init__(self, rules, evidence_list, windows, ppid_of):
        super().__init__(rules)
        self._evidence = list(evidence_list)
        self._windows = list(windows)
        self._ppid_of = ppid_of

    def evidence(self):
        return list(self._evidence)

    def window_for_pid(self, pid):
        return window_for_pid(self._windows, pid, self._ppid_of)

    def describe(self):
        return {"platform": self.platform}


def scan_shape(shape, dump, windows, ppid_of, rules):
    detector = FakeDetector(rules, parse_evidence(dump(shape)), windows, ppid_of)
    return detector.scan(now=datetime(2026, 9, 15, 10, 30, tzinfo=OSLO))


# -- condition 1: is a call live -------------------------------------------


def test_native_client_holds_both_streams(dump):
    (call,) = [e for e in parse_evidence(dump("teams-native")) if e.is_call]
    assert call.pid == 4101
    assert call.has_playback and call.has_capture
    assert call.client_name == "Teams"
    assert call.binary == "teams-for-linux"


def test_playback_without_capture_is_not_a_call(dump):
    evidence = parse_evidence(dump("browser-playback-only"))
    browser = next(e for e in evidence if e.pid == 4410)
    assert browser.has_playback
    assert not browser.has_capture
    assert not browser.is_call
    assert not [e for e in evidence if e.is_call]


def test_teams_tab_with_no_call_is_not_a_call(dump):
    assert not [e for e in parse_evidence(dump("teams-tab-no-call")) if e.is_call]


def test_the_shell_tap_is_never_a_call(dump):
    """quickshell holds a capture stream permanently; it is not an application."""
    assert parse_evidence(dump("idle")) == []


def test_identity_comes_from_the_client_not_the_node(dump):
    """Chrome stream nodes carry no pid at all; only client.id links them."""
    clients = parse_clients(dump("teams-tab"))
    assert clients[103].pid == 4310
    assert clients[103].binary == "chrome"
    (call,) = [e for e in parse_evidence(dump("teams-tab")) if e.is_call]
    assert call.pid == 4310
    assert call.client_name == "Chromium"


def test_playback_handle_is_the_output_serial(dump):
    (call,) = [e for e in parse_evidence(dump("teams-pwa")) if e.is_call]
    assert call.playback_handle == "1201"


def test_highest_serial_wins_when_two_tabs_make_noise(dump):
    (call,) = [e for e in parse_evidence(dump("two-playback-streams")) if e.is_call]
    assert call.playback_handle == "1699"


def test_evidence_is_one_row_per_process(dump):
    evidence = parse_evidence(dump("teams-native"))
    assert [e.pid for e in evidence] == [4101]


# -- condition 2: is it Teams ----------------------------------------------


@pytest.mark.parametrize(
    ("shape", "app_id", "matched_by"),
    [
        ("teams-native", "teams-native", "pipewire"),
        ("teams-pwa", "teams-pwa", "window_class"),
        ("teams-tab", "teams-tab", "window_title"),
    ],
)
def test_each_teams_shape_triggers(shape, app_id, matched_by, dump, windows, ppid_of, rules):
    (call,) = scan_shape(shape, dump, windows, ppid_of, rules)
    assert call.identity is not None
    assert (call.identity.app_id, call.identity.matched_by) == (app_id, matched_by)
    assert call.identity.label == "Microsoft Teams"


def test_a_video_does_not_trigger(dump, windows, ppid_of, rules):
    assert scan_shape("browser-playback-only", dump, windows, ppid_of, rules) == []


def test_an_open_teams_tab_does_not_trigger(dump, windows, ppid_of, rules):
    assert scan_shape("teams-tab-no-call", dump, windows, ppid_of, rules) == []


def test_an_unknown_call_is_reported_without_an_identity(dump, windows, ppid_of):
    """munin doctor must be able to show a live call Munin chose not to act on."""
    (call,) = scan_shape("teams-tab", dump, windows, ppid_of, [])
    assert call.identity is None
    assert call.evidence.is_call


def test_pipewire_beats_the_window_when_both_match(windows, ppid_of):
    """A native client is unambiguous, so it must win over a title substring."""
    rules = [
        AppRule(app_id="teams-tab", label="Teams (tab)", window_title_contains="Microsoft Teams"),
        AppRule(app_id="teams-native", label="Teams (native)", client_name="Teams"),
    ]
    evidence = CallEvidence(
        pid=4101, has_playback=True, has_capture=True, client_name="Teams", binary="teams-for-linux"
    )
    window = window_for_pid(windows, 4101, ppid_of)
    identity = identify(evidence, window, rules)
    assert identity is not None
    assert (identity.app_id, identity.matched_by) == ("teams-native", "pipewire")


def test_detection_degrades_to_pipewire_only_without_a_window(dump, rules):
    """An unreachable compositor must cost the title shape, not the native one."""
    native = next(e for e in parse_evidence(dump("teams-native")) if e.is_call)
    tab = next(e for e in parse_evidence(dump("teams-tab")) if e.is_call)
    assert identify(native, None, rules) is not None
    assert identify(tab, None, rules) is None


# -- the window walk --------------------------------------------------------


def test_window_walk_finds_the_parent_of_the_audio_process(windows, ppid_of):
    window = window_for_pid(windows, 4310, ppid_of)
    assert window is not None
    assert window.pid == 4300
    assert window.window_class == "google-chrome"
    assert "Microsoft Teams" in (window.title or "")


def test_window_walk_matches_the_pid_itself_first(windows, ppid_of):
    assert window_for_pid(windows, 4700, ppid_of) == WindowInfo(
        pid=4700, window_class="Alacritty", title="munin - workstation"
    )


def test_window_walk_is_bounded(windows):
    """A pid chain that never reaches a window must stop, not loop."""
    calls: list[int] = []

    def ppid_of(pid: int) -> int:
        calls.append(pid)
        return pid + 1

    assert window_for_pid(windows, 90000, ppid_of, max_hops=5) is None
    assert len(calls) <= 5


def test_window_walk_survives_a_cycle(windows):
    assert window_for_pid(windows, 90000, lambda pid: 90000) is None


def test_window_walk_stops_at_init(windows):
    """A stream held by a process whose parent is init owns no window."""
    assert window_for_pid(windows, 4410, lambda pid: 1) is None


def test_read_ppid_reads_proc_status(tmp_path):
    status = tmp_path / "4310" / "status"
    status.parent.mkdir()
    status.write_text("Name:\tchrome\nTgid:\t4310\nPid:\t4310\nPPid:\t4300\n", encoding="utf-8")
    assert read_ppid(4310, proc=tmp_path) == 4300
    assert read_ppid(999999, proc=tmp_path) is None


def test_parse_windows_skips_rows_without_a_pid():
    assert parse_windows([{"class": "x", "title": "y", "pid": -1}]) == []


def test_parse_windows_falls_back_to_the_initial_class():
    (window,) = parse_windows([{"pid": 7, "class": "", "initialClass": "google-chrome"}])
    assert window.window_class == "google-chrome"


# -- default source ---------------------------------------------------------


def test_default_source_name_reads_the_metadata_object(dump):
    name = default_source_name(dump("idle"))
    assert name == "alsa_input.usb-Synthetic_Headset-00.mono-fallback"


def test_default_source_name_is_none_without_metadata():
    assert default_source_name([]) is None


# -- the poll loop ----------------------------------------------------------


class ScriptedDetector(Detector):
    """Replays a list of scan results, one per poll."""

    platform = "linux"

    def __init__(self, rules, scripted):
        super().__init__(rules)
        self._scripted = list(scripted)
        self.scans = 0

    def evidence(self):
        return []

    def window_for_pid(self, pid):
        return None

    def describe(self):
        return {}

    def scan(self, *, now=None):
        result = self._scripted[min(self.scans, len(self._scripted) - 1)]
        self.scans += 1
        return result


def test_watcher_emits_one_start_and_one_end(dump, windows, ppid_of, rules):
    live = scan_shape("teams-tab", dump, windows, ppid_of, rules)
    detector = ScriptedDetector(rules, [live, live, []])
    watcher = CallWatcher(detector)

    started = watcher.poll()
    assert [e.event for e in started] == ["call-started"]
    assert started[0].pid == 4310
    assert started[0].app_id == "teams-tab"
    assert started[0].handle == "1301"
    assert "Microsoft Teams" in (started[0].title or "")

    assert watcher.poll() == []          # still live: no edge, no event
    assert watcher.live_pids == frozenset({4310})

    ended = watcher.poll()
    assert [e.event for e in ended] == ["call-ended"]
    # The identity it had while it was live survives into the end event.
    assert ended[0].app_id == "teams-tab"
    assert watcher.live_pids == frozenset()


def test_watcher_ignores_unidentified_calls_by_default(dump, windows, ppid_of):
    live = scan_shape("teams-tab", dump, windows, ppid_of, [])
    watcher = CallWatcher(ScriptedDetector([], [live]))
    assert watcher.poll() == []


def test_watcher_can_be_asked_for_unidentified_calls(dump, windows, ppid_of):
    live = scan_shape("teams-tab", dump, windows, ppid_of, [])
    watcher = CallWatcher(ScriptedDetector([], [live]), identified_only=False)
    (event,) = watcher.poll()
    assert event.app_id is None
    assert event.pid == 4310


def test_watcher_survives_a_broken_detector(rules):
    class Broken(ScriptedDetector):
        def scan(self, *, now=None):
            raise RuntimeError("pw-dump went away")

    assert CallWatcher(Broken(rules, [[]])).poll() == []


def test_watcher_run_stops_after_max_polls(dump, windows, ppid_of, rules):
    live = scan_shape("teams-tab", dump, windows, ppid_of, rules)
    detector = ScriptedDetector(rules, [live, live, []])
    seen: list[str] = []
    CallWatcher(detector).run(0.0, lambda event: seen.append(event.event), max_polls=3)
    assert seen == ["call-started", "call-ended"]


def test_watcher_run_survives_a_broken_handler(dump, windows, ppid_of, rules):
    live = scan_shape("teams-native", dump, windows, ppid_of, rules)
    detector = ScriptedDetector(rules, [live])

    def handler(event):
        raise RuntimeError("the daemon socket was busy")

    CallWatcher(detector).run(0.0, handler, max_polls=1)  # must not raise


# -- the live detector's plumbing (no hardware) -----------------------------


def test_detector_degrades_when_pw_dump_is_missing(rules):
    detector = PipewireDetector(rules, pw_dump="munin-no-such-tool")
    assert detector.evidence() == []
    assert "not installed" in detector.describe()["pw_error"]


def test_detector_degrades_when_the_compositor_is_unreachable(rules, monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/nonexistent-munin-runtime")
    detector = PipewireDetector(rules)
    assert detector.window_for_pid(4310) is None
    assert "HYPRLAND_INSTANCE_SIGNATURE" in detector.describe()["wm_error"]


def test_hyprland_signature_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "from-env")
    assert hyprland_signature(tmp_path) == "from-env"


def test_hyprland_signature_resolves_a_single_instance_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    (tmp_path / "hypr" / "abc123_1_2").mkdir(parents=True)
    assert hyprland_signature(tmp_path) == "abc123_1_2"


def test_hyprland_signature_declines_to_guess_between_two(monkeypatch, tmp_path):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    (tmp_path / "hypr" / "one").mkdir(parents=True)
    (tmp_path / "hypr" / "two").mkdir(parents=True)
    assert hyprland_signature(tmp_path) is None


def test_detector_describe_is_flat_strings(rules):
    described = PipewireDetector(rules).describe()
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in described.items())
    assert described["rules"] == "3"
