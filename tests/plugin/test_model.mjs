// Tests for plugin/local.munin/Model.js.
//
// Run with:  node tests/plugin/test_model.mjs
//
// There is no QML test runner on this machine and `qs` needs a compositor, so
// the pure half of the plugin is tested here instead: state parsing, the
// seven bar states, elapsed formatting, the three-shape detection table and
// the started/ended diff. Everything that needs PipeWire or a running shell
// is a live check, described in plugin/local.munin/README.md.
//
// Deliberately dependency-free: node:test and node:assert only, so this runs
// on the system node with nothing installed.

import test from "node:test";
import assert from "node:assert/strict";
import { loadModel } from "./model_shim.mjs";

const M = loadModel();

// A fixed instant, so nothing here depends on when it runs.
const T0 = Date.parse("2026-09-14T13:25:08+02:00");

function stateJson(over = {}) {
  return JSON.stringify(
    Object.assign(
      {
        schema_version: 1,
        state: "recording",
        since: "2026-09-14T13:25:08+02:00",
        started_at: "2026-09-14T13:25:08+02:00",
        title: "Weekly quality sync",
        session: "/home/user/munin/recordings/2026/09/2026-09-14T1325-weekly-quality-sync",
        session_id: "2026-09-14T1325-weekly-quality-sync",
        segment: 1,
        detected_app: { app_id: "teams-tab", label: "Microsoft Teams", pid: 37022 },
        grace_deadline: null,
        queue_depth: 2,
        last_error: null,
        updated_at: "2026-09-14T13:25:08+02:00",
        daemon_pid: 4211,
      },
      over,
    ),
  );
}

// ------------------------------------------------------------- parseState

test("parseState reads the contract's state.json", () => {
  const v = M.parseState(stateJson());
  assert.equal(v.loaded, true);
  assert.equal(v.state, "recording");
  assert.equal(v.title, "Weekly quality sync");
  assert.equal(v.session_id, "2026-09-14T1325-weekly-quality-sync");
  assert.equal(v.segment, 1);
  assert.equal(v.queue_depth, 2);
  assert.equal(v.daemon_pid, 4211);
  assert.deepEqual(v.detected_app, {
    app_id: "teams-tab",
    label: "Microsoft Teams",
    pid: 37022,
  });
});

test("parseState never throws on junk, it degrades to idle", () => {
  for (const bad of ["", "   ", "{", "null", "[]", "not json at all", undefined]) {
    const v = M.parseState(bad);
    assert.equal(v.state, "idle", `input: ${JSON.stringify(bad)}`);
    assert.equal(v.loaded, false);
    assert.equal(v.queue_depth, 0);
  }
});

test("parseState rejects a state name outside the contract", () => {
  assert.equal(M.parseState(stateJson({ state: "uploading" })).state, "idle");
});

test("parseState never yields a null title", () => {
  assert.equal(M.parseState(stateJson({ title: null })).title, "");
});

// ---------------------------------------------------------- effectiveState

test("done expires after 30 s and reads as idle", () => {
  const json = stateJson({ state: "done", since: "2026-09-14T13:25:08+02:00" });
  const v = M.parseState(json);
  assert.equal(M.effectiveState(v, T0 + 10_000), "done");
  assert.equal(M.effectiveState(v, T0 + 29_000), "done");
  assert.equal(M.effectiveState(v, T0 + 31_000), "idle");
});

test("failed never expires -- a failure that hides itself is a lost meeting", () => {
  const v = M.parseState(stateJson({ state: "failed", since: "2026-09-14T13:25:08+02:00" }));
  assert.equal(M.effectiveState(v, T0 + 86_400_000), "failed");
});

// ---------------------------------------------------------------- time

test("elapsedSeconds counts from an offset timestamp", () => {
  assert.equal(M.elapsedSeconds("2026-09-14T13:25:08+02:00", T0 + 42 * 1000), 42);
  assert.equal(M.elapsedSeconds("2026-09-14T11:25:08+00:00", T0 + 42 * 1000), 42);
});

test("elapsedSeconds returns -1 for an unreadable timestamp and never goes negative", () => {
  assert.equal(M.elapsedSeconds(null, T0), -1);
  assert.equal(M.elapsedSeconds("whenever", T0), -1);
  assert.equal(M.elapsedSeconds("2026-09-14T13:25:08+02:00", T0 - 5000), 0);
});

test("formatElapsed is HH:MM:SS with unbounded hours", () => {
  assert.equal(M.formatElapsed(0), "00:00:00");
  assert.equal(M.formatElapsed(42 * 60 + 11), "00:42:11");
  assert.equal(M.formatElapsed(3600), "01:00:00");
  assert.equal(M.formatElapsed(26 * 3600 + 4 * 60 + 11), "26:04:11");
});

test("formatElapsedShort drops the hour field under an hour", () => {
  assert.equal(M.formatElapsedShort(42 * 60 + 11), "42:11");
  assert.equal(M.formatElapsedShort(3600 + 14 * 60), "1:14:00");
});

test("formatDuration reads as words", () => {
  assert.equal(M.formatDuration(9), "9 s");
  assert.equal(M.formatDuration(52 * 60), "52 min");
  assert.equal(M.formatDuration(3600 + 14 * 60), "1 h 14 min");
  assert.equal(M.formatDuration(7200), "2 h");
  assert.equal(M.formatDuration(3600 + 3599), "2 h");
});

// ----------------------------------------------------------------- bar

test("the seven states of spec 9.2 each render one way", () => {
  assert.equal(M.visible("idle"), true);
  for (const s of ["detected", "recording", "ending", "captured", "transcribing", "done", "failed"]) {
    assert.equal(M.visible(s), true, s);
  }

  assert.equal(M.barGlyph("detected"), M.GLYPH);
  assert.equal(M.barGlyph("recording"), "");
  assert.equal(M.barGlyph("ending"), "");
  assert.equal(M.barGlyph("transcribing"), M.GLYPH_WORKING);
  assert.equal(M.barGlyph("done"), M.GLYPH_DONE);
  assert.equal(M.barGlyph("failed"), M.GLYPH_FAILED);

  assert.equal(M.barShowsDot("recording"), true);
  assert.equal(M.barShowsDot("ending"), true);
  assert.equal(M.barShowsDot("detected"), false);

  assert.equal(M.barPulses("recording"), true);
  assert.equal(M.barPulses("ending"), false);

  assert.equal(M.barSpins("transcribing"), true);
  // `captured` with no worker behind it is the PoC's designed end state and it
  // lasts forever: a spinner there is an animation that never stops.
  assert.equal(M.barSpins("captured"), false);
  assert.equal(M.barSpins("done"), false);

  assert.equal(M.barTone("recording"), "urgent");
  assert.equal(M.barTone("ending"), "urgent");
  assert.equal(M.barTone("failed"), "urgent");
  assert.equal(M.barTone("detected"), "dim");
  assert.equal(M.barTone("transcribing"), "foreground");
});

test("the identity glyph is the level bars U+F0EA2, legible at 13 px", () => {
  assert.equal(M.GLYPH.codePointAt(0), 0xf0ea2);
  assert.equal([...M.GLYPH].length, 1);
});

// Each of these was chosen for the bar's 13 px icon font, where a hairline
// stroke vanishes. Pinning the code points keeps a later "nicer" glyph from
// quietly undoing that: md-check and md-alert_outline are the thin cuts this
// replaced, and md-loading is the arc that all but disappeared while spinning.
test("every bar glyph is one code point, and none is a hairline cut", () => {
  const marks = {
    GLYPH: 0xf0ea2,            // md-equalizer
    GLYPH_WORKING: 0xf0450,    // md-refresh, not md-loading 0xf0772
    GLYPH_DONE: 0xf0e1e,       // md-check_bold, not md-check 0xf012c
    GLYPH_FAILED: 0xf0026,     // md-alert, not md-alert_outline 0xf002a
    GLYPH_STOP: 0xf04db,       // md-stop
    GLYPH_FOLDER: 0xf024b,     // md-folder
  };
  const seen = new Set();
  for (const [name, cp] of Object.entries(marks)) {
    assert.equal([...M[name]].length, 1, name);
    assert.equal(M[name].codePointAt(0), cp, name);
    assert.equal(seen.has(cp), false, name + " is not distinct");
    seen.add(cp);
  }
});

test("barLabel says the right thing in each state", () => {
  // The heartbeat keeps pace with the clock, or the view would read as stale.
  const rec = M.parseState(
    stateJson({ updated_at: "2026-09-14T14:07:00+02:00" }),
  );
  assert.equal(M.barLabel(rec, T0 + (42 * 60 + 11) * 1000), "00:42:11");

  const det = M.parseState(stateJson({ state: "detected", started_at: null }));
  assert.equal(M.barLabel(det, T0), "Microsoft Teams");

  const work = M.parseState(stateJson({ state: "transcribing", queue_depth: 2 }));
  assert.equal(M.barLabel(work, T0), "2 queued");

  const done = M.parseState(stateJson({ state: "done" }));
  assert.equal(M.barLabel(done, T0), "Transcript ready");
  assert.equal(M.barLabel(done, T0 + 31_000), "");

  const failed = M.parseState(stateJson({ state: "failed" }));
  assert.equal(M.barLabel(failed, T0), "Retry");
});

test("tooltip in failed carries the daemon's reason", () => {
  const v = M.parseState(stateJson({ state: "failed", last_error: "no space left" }));
  assert.equal(M.tooltipText(v, T0), "Failed: no space left");
});

// --------------------------------------------------------------- actions

test("the primary action follows the state", () => {
  assert.deepEqual(M.primaryAction("idle"), { label: "Record", argv: ["munin", "start"] });
  assert.deepEqual(M.primaryAction("detected"), {
    label: "Record",
    argv: ["munin", "start", "--from-detection"],
  });
  assert.deepEqual(M.primaryAction("recording"), {
    label: "Stop and transcribe",
    argv: ["munin", "stop"],
  });
  assert.deepEqual(M.primaryAction("ending"), {
    label: "Stop and transcribe",
    argv: ["munin", "stop"],
  });
  assert.deepEqual(M.primaryAction("done"), {
    label: "Resume",
    argv: ["munin", "start", "--resume"],
  });
});

test("muninArgv keeps a hostile window title as one literal argument", () => {
  const title = '$(rm -rf ~) "; reboot #';
  const argv = M.muninArgv(["munin", "event", "call-started", "--title", title]);
  assert.deepEqual(argv.slice(0, 4), ["bash", "-lc", 'exec "$@"', "bash"]);
  assert.equal(argv[argv.length - 1], title);
  assert.equal(argv.length, 9);
});

// -------------------------------------------------------------- sessions

test("parseSessions reads the CLI's list payload", () => {
  const payload = JSON.stringify({
    sessions: [
      {
        id: "2026-09-14T1325-weekly-quality-sync",
        state: "pending",
        title: "Weekly quality sync",
        started_at: "2026-09-14T13:25:08+02:00",
        duration_seconds: 3140,
        path: "/home/user/munin/recordings/2026/09/2026-09-14T1325-weekly-quality-sync",
        pending_reason: "no transcription backend configured",
      },
    ],
  });
  const list = M.parseSessions(payload);
  assert.equal(list.length, 1);
  assert.equal(list[0].title, "Weekly quality sync");
  assert.equal(list[0].duration_seconds, 3140);
  assert.match(M.sessionMeta(list[0]), /52 min/);
  assert.match(M.sessionMeta(list[0]), /no transcription backend configured/);
});

test("parseSessions survives junk and a missing title", () => {
  assert.deepEqual(M.parseSessions("nonsense"), []);
  assert.deepEqual(M.parseSessions('{"sessions": null}'), []);
  const list = M.parseSessions('{"sessions": [{"id": "adhoc-1", "state": "pending"}]}');
  assert.equal(list[0].title, "adhoc-1");
});

// ------------------------------------------------------------- detection

test("identify implements the three-shape table in match order", () => {
  // Native teams-for-linux: PipeWire alone is enough.
  assert.deepEqual(M.identify("Teams", "teams-for-linux", "Chat | Teams", null), {
    app_id: "teams-native",
    label: "Microsoft Teams",
    matched_by: "pipewire",
  });

  // Installed PWA: PipeWire says Chromium, the window class identifies it.
  assert.deepEqual(
    M.identify("Chromium", "chrome-teams.microsoft.com__-Default", "Calendar", null),
    { app_id: "teams-pwa", label: "Microsoft Teams", matched_by: "window_class" },
  );

  // Browser tab: only the title, and case-insensitively.
  assert.deepEqual(M.identify("Chromium", "google-chrome", "Call | microsoft TEAMS", null), {
    app_id: "teams-tab",
    label: "Microsoft Teams",
    matched_by: "window_title",
  });
});

test("identify returns null for a call that is not a known app", () => {
  assert.equal(M.identify("Chromium", "google-chrome", "Some video", null), null);
  assert.equal(M.identify("", "", "", null), null);
});

test("identify tries every rule's PipeWire name before any window class", () => {
  // A rule list where the title rule comes first: the PipeWire stage still
  // wins, which is the whole point of running the stages in order.
  const rules = [
    { app_id: "beacon-tab", label: "Beacon 365", window_title_contains: "Beacon" },
    { app_id: "beacon-native", label: "Beacon 365", client_name: "Beacon" },
  ];
  assert.equal(M.identify("Beacon", "x", "Beacon 365 call", rules).app_id, "beacon-native");
});

test("liveCalls is condition 1: a playback and a capture stream on one owner", () => {
  const owners = {
    "pid:37022": {
      pid: 37022,
      client_name: "Chromium",
      playback: true,
      capture: true,
      playback_handle: "91",
    },
    "pid:41000": { pid: 41000, client_name: "mpv", playback: true, capture: false },
    "client:12": { pid: 0, client_name: "pw-cat", playback: false, capture: true },
  };
  const calls = M.liveCalls(owners);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].pid, 37022);
  assert.equal(calls[0].playback_handle, "91");
});

test("liveCalls keeps an owner PipeWire gave no pid for", () => {
  const calls = M.liveCalls({
    "client:12": { pid: 0, client_name: "Teams", playback: true, capture: true },
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].key, "client:12");
  assert.equal(calls[0].pid, 0);
});

test("diffCalls fires once per transition, not once per scan", () => {
  const a = { key: "pid:1" };
  const b = { key: "pid:2" };

  let d = M.diffCalls([], [a]);
  assert.deepEqual(d.started.map((c) => c.key), ["pid:1"]);
  assert.deepEqual(d.ended, []);

  d = M.diffCalls([a], [a]);
  assert.deepEqual(d.started, []);
  assert.deepEqual(d.ended, []);

  d = M.diffCalls([a], [a, b]);
  assert.deepEqual(d.started.map((c) => c.key), ["pid:2"]);

  d = M.diffCalls([a, b], [b]);
  assert.deepEqual(d.ended.map((c) => c.key), ["pid:1"]);

  d = M.diffCalls([a], []);
  assert.deepEqual(d.ended.map((c) => c.key), ["pid:1"]);
});

test("callEventArgv omits a pid PipeWire did not publish", () => {
  const withPid = M.callEventArgv(
    "call-started",
    { key: "pid:37022", pid: 37022 },
    { app_id: "teams-tab" },
    "Call | Microsoft Teams",
  );
  assert.deepEqual(withPid, [
    "munin",
    "event",
    "call-started",
    "--pid",
    "37022",
    "--app-id",
    "teams-tab",
    "--title",
    "Call | Microsoft Teams",
  ]);

  const withoutPid = M.callEventArgv("call-started", { key: "client:12", pid: 0 }, null, "");
  assert.deepEqual(withoutPid, ["munin", "event", "call-started"]);

  // call-ended carries no title: the daemon already knows the session.
  const ended = M.callEventArgv("call-ended", { pid: 37022 }, null, "Call | Microsoft Teams");
  assert.deepEqual(ended, ["munin", "event", "call-ended", "--pid", "37022"]);
});

test("callEventArgv carries the label, the rule id and the playback handle", () => {
  // The handle is what binds the app track. Without it the daemon has no app
  // target and the capturer writes a silent app.opus beside the microphone.
  const argv = M.callEventArgv(
    "call-started",
    { key: "pid:37022", pid: 37022, playback_handle: "91" },
    { app_id: "teams-tab", label: "Microsoft Teams" },
    "Call | Microsoft Teams",
  );
  assert.deepEqual(argv, [
    "munin",
    "event",
    "call-started",
    "--pid",
    "37022",
    "--app",
    "Microsoft Teams",
    "--app-id",
    "teams-tab",
    "--handle",
    "91",
    "--title",
    "Call | Microsoft Teams",
  ]);

  // call-ended needs neither: the daemon already knows which session it is.
  const ended = M.callEventArgv(
    "call-ended",
    { pid: 37022, playback_handle: "91" },
    null,
    "",
  );
  assert.deepEqual(ended, ["munin", "event", "call-ended", "--pid", "37022"]);
});

test("identify matches case-insensitively and on the binary, as the daemon does", () => {
  // PipeWire's application.name is whatever the app set; the daemon casefolds.
  assert.equal(M.identify("teams", "", "", null).app_id, "teams-native");
  assert.equal(
    M.identify("Chromium", "CHROME-teams.microsoft.com__-Default", "", null).app_id,
    "teams-pwa",
  );

  // A rule with only a binary -- the shape `detect/base.identify` supports and
  // the plugin used to ignore entirely, so a new row simply did nothing.
  const rules = [{ app_id: "beacon", label: "Beacon 365", binary: "beacon" }];
  assert.deepEqual(M.identify("", "", "", rules, "Beacon"), {
    app_id: "beacon",
    label: "Beacon 365",
    matched_by: "pipewire",
  });
  assert.equal(M.identify("", "", "", rules, "chrome"), null);
});

test("the daemon's [[detection.apps]] table beats the compiled-in fallback", () => {
  const v = M.parseState(
    stateJson({
      detection_rules: [
        { app_id: "beacon-native", label: "Beacon 365", client_name: "Beacon" },
        { app_id: "bad", label: "" },
        "nonsense",
      ],
    }),
  );
  assert.deepEqual(M.rulesFor(v), [
    { app_id: "beacon-native", label: "Beacon 365", client_name: "Beacon" },
  ]);
  assert.equal(M.identify("Beacon", "", "", M.rulesFor(v)).app_id, "beacon-native");

  // A daemon that published nothing leaves the plugin on its own table.
  assert.equal(M.rulesFor(M.parseState(stateJson())), M.DEFAULT_APP_RULES);
  assert.equal(M.rulesFor(null), M.DEFAULT_APP_RULES);
});

// ------------------------------------------------------------- staleness

test("a state file the daemon stopped refreshing is not a live recording", () => {
  const fresh = M.parseState(stateJson());
  assert.equal(M.isStale(fresh, T0 + 60_000), false);
  assert.equal(M.effectiveState(fresh, T0 + 60_000), "recording");

  // Three missed 30 s heartbeats: munin-rec was killed, and a pulsing dot with
  // a climbing clock would claim a recording that died with it.
  const stale = M.parseState(stateJson());
  assert.equal(M.isStale(stale, T0 + 91_000), true);
  assert.equal(M.effectiveState(stale, T0 + 91_000), "failed");
  assert.match(M.tooltipText(stale, T0 + 91_000), /stopped answering/);

  // A settled state stays true whatever the heartbeat says.
  const captured = M.parseState(stateJson({ state: "captured" }));
  assert.equal(M.effectiveState(captured, T0 + 3_600_000), "captured");

  // No file at all is absent, not stale.
  assert.equal(M.isStale(M.parseState("{"), T0), false);
});

test("the grace period offers Keep recording, which nothing else can", () => {
  assert.deepEqual(M.secondaryAction("ending"), {
    label: "Keep recording",
    argv: ["munin", "event", "call-started"],
  });
  for (const s of ["idle", "detected", "recording", "captured", "done", "failed"]) {
    assert.equal(M.secondaryAction(s), null, s);
  }
});

// --------------------------------------------------------- shape contract

test("Model.js exports every function the QML calls", () => {
  for (const name of [
    "parseState",
    "emptyState",
    "effectiveState",
    "elapsedSeconds",
    "formatElapsed",
    "formatElapsedShort",
    "formatDuration",
    "formatClock",
    "visible",
    "barGlyph",
    "barTone",
    "barPulses",
    "barShowsDot",
    "barSpins",
    "barLabel",
    "tooltipText",
    "primaryAction",
    "muninArgv",
    "parseSessions",
    "sessionGlyph",
    "sessionMeta",
    "identify",
    "liveCalls",
    "diffCalls",
    "callEventArgv",
    "isStale",
    "rulesFor",
    "parseRules",
    "secondaryAction",
  ]) {
    assert.equal(typeof M[name], "function", name);
  }
  assert.equal(M.STATES.length, 8);
  assert.equal(M.DEFAULT_APP_RULES.length, 3);
});


// ------------------------------------------------ deferred transcription

test("a pending session is waiting, not failed", () => {
  const list = M.parseSessions('{"sessions": [{"id": "a", "state": "pending", "pending_reason": "no transcription backend configured"}]}');
  assert.equal(list[0].state, "pending");
  assert.equal(M.sessionGlyph("pending"), M.GLYPH);
  assert.equal(M.sessionGlyph("captured"), M.GLYPH);
  assert.equal(M.sessionGlyph("failed"), M.GLYPH_FAILED);
  // A state the plugin has never heard of is still flagged, never hidden.
  const odd = M.parseSessions('{"sessions": [{"id": "b", "state": "exploded"}]}');
  assert.equal(odd[0].state, "unknown");
  assert.equal(M.sessionGlyph("unknown"), M.GLYPH_FAILED);
});

test("with no backend the bar shows static level bars and no count", () => {
  const view = M.parseState(stateJson({ state: "captured", queue_depth: 3,
    transcription_backend: "none", updated_at: "2026-09-14T13:25:08+02:00" }));
  assert.equal(M.deferred(view, T0), true);
  assert.equal(M.barGlyphFor(view, T0), M.GLYPH);
  assert.equal(M.barToneFor(view, T0), "dim");
  assert.equal(M.barLabel(view, T0), "");
  assert.equal(M.barSpins(M.effectiveState(view, T0)), false);
  assert.match(M.tooltipText(view, T0), /no transcription backend/);
  assert.match(M.tooltipText(view, T0), /3 captured/);
});

test("with a real backend the queue still reads as work", () => {
  const view = M.parseState(stateJson({ state: "transcribing", queue_depth: 2,
    transcription_backend: "local", updated_at: "2026-09-14T13:25:08+02:00" }));
  assert.equal(M.deferred(view, T0), false);
  assert.equal(M.barGlyphFor(view, T0), M.GLYPH_WORKING);
  assert.equal(M.barToneFor(view, T0), "foreground");
  assert.equal(M.barLabel(view, T0), "2 queued");
});

test("a state file without the field behaves as before", () => {
  const view = M.parseState(stateJson({ state: "captured", queue_depth: 1,
    updated_at: "2026-09-14T13:25:08+02:00" }));
  assert.equal(view.transcription_backend, null);
  assert.equal(M.deferred(view, T0), false);
  assert.equal(M.barLabel(view, T0), "1 queued");
});


// ------------------------------------------------- resume vs new recording

test("inside the resume window the panel offers both Resume and New recording", () => {
  const view = M.parseState(stateJson({ state: "captured", queue_depth: 1,
    since: "2026-09-14T13:25:08+02:00", resume_window_seconds: 600,
    updated_at: "2026-09-14T13:25:08+02:00" }));
  const at = T0 + 3 * 60 * 1000;           // three minutes after capture
  assert.equal(M.resumeSecondsLeft(view, at), 420);
  assert.deepEqual(M.primaryActionFor(view, at),
    { label: "Resume · 7 min left", argv: ["munin", "start", "--resume"] });
  assert.deepEqual(M.secondaryActionFor(view, at),
    { label: "New recording", argv: ["munin", "start"] });
});

test("past the window there is one button and it says New recording", () => {
  const view = M.parseState(stateJson({ state: "captured", queue_depth: 1,
    since: "2026-09-14T13:25:08+02:00", resume_window_seconds: 600,
    updated_at: "2026-09-14T13:25:08+02:00" }));
  const at = T0 + 11 * 60 * 1000;
  assert.equal(M.resumeSecondsLeft(view, at), 0);
  assert.deepEqual(M.primaryActionFor(view, at), { label: "New recording", argv: ["munin", "start"] });
  assert.equal(M.secondaryActionFor(view, at), null);
});

test("under a minute the label counts seconds", () => {
  const view = M.parseState(stateJson({ state: "failed", queue_depth: 0,
    since: "2026-09-14T13:25:08+02:00", resume_window_seconds: 600,
    updated_at: "2026-09-14T13:25:08+02:00" }));
  assert.equal(M.primaryActionFor(view, T0 + 570 * 1000).label, "Resume · 30 s left");
});

test("a daemon that publishes no window keeps the old buttons", () => {
  const view = M.parseState(stateJson({ state: "captured", queue_depth: 1,
    since: "2026-09-14T13:25:08+02:00", updated_at: "2026-09-14T13:25:08+02:00" }));
  assert.equal(M.resumeSecondsLeft(view, T0), -1);
  assert.deepEqual(M.primaryActionFor(view, T0), M.primaryAction("captured"));
  assert.equal(M.secondaryActionFor(view, T0), null);
});

test("the live states are untouched by the resume logic", () => {
  const rec = M.parseState(stateJson({ state: "recording", resume_window_seconds: 600,
    updated_at: "2026-09-14T13:25:08+02:00" }));
  assert.deepEqual(M.primaryActionFor(rec, T0), M.primaryAction("recording"));
  const ending = M.parseState(stateJson({ state: "ending", resume_window_seconds: 600,
    updated_at: "2026-09-14T13:25:08+02:00" }));
  assert.deepEqual(M.secondaryActionFor(ending, T0), M.secondaryAction("ending"));
});

// Idle remains an entry point after a transient completion indicator expires.
test("idle and saved audio use static level bars; only real transcription spins", () => {
  assert.equal(M.barGlyph("idle"), M.GLYPH);
  assert.equal(M.barGlyph("captured"), M.GLYPH);
  for (const state of ["idle", "captured", "transcribing"]) {
    const view = M.parseState(stateJson({state, transcription_backend: "none",
      updated_at: "2026-09-14T13:25:08+02:00"}));
    assert.equal(M.barGlyphFor(view, T0), M.GLYPH);
    assert.equal(M.barSpinsFor(view, T0), false);
  }
  const working = M.parseState(stateJson({state: "transcribing", transcription_backend: "local",
    updated_at: "2026-09-14T13:25:08+02:00"}));
  assert.equal(M.barSpinsFor(working, T0), true);
  const done = M.parseState(stateJson({state: "done", since: "2026-09-14T13:25:08+02:00",
    updated_at: "2026-09-14T13:25:08+02:00"}));
  const later = T0 + (M.DONE_VISIBLE_SECONDS + 1) * 1000;
  assert.equal(M.barGlyphFor(done, later), M.GLYPH);
  assert.equal(M.visible(M.effectiveState(done, later)), true);
});
