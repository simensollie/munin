// Pure state helpers for the Munin plugin: parsing state.json, formatting
// elapsed time, mapping a state to a glyph, a tone and a label, and deciding
// which calls started or ended between two PipeWire scans.
//
// Kept out of the QML so the same logic is readable in one place, has no
// dependency on the shell's object graph, and can be exercised by
// `node tests/plugin/test_model.mjs` without a running shell.
//
// Nothing here reads a file, spawns a process or touches the network. QML
// hands it strings and plain objects and gets plain objects back. Colours are
// never named: a state maps to a *tone* ("urgent", "foreground", "dim") and
// the QML resolves that against the theme singleton.
//
// Owner: plugin workstream. Contract:
// docs/superpowers/specs/2026-09-14-poc-contracts.md, sections 6, 7.2 and 9,
// and spec section 9.2.

.pragma library

// ---------------------------------------------------------------- constants

var SCHEMA_VERSION = 1;

// Bar states, in the order of spec section 9.2. `captured` is written by the
// daemon between `ending` and the worker picking the session up; the bar
// renders it the same way as `transcribing`, because to the user it is the
// same fact: capture is done and safe on disk.
var STATES = ["idle", "detected", "recording", "ending",
              "captured", "transcribing", "done", "failed"];

// Session states are the spool's, not the bar's: `pending` is a session that
// is captured, safe and waiting for a backend. It is not a bar state (the
// daemon publishes it as `captured`), and it is emphatically not `failed` --
// mapping it there painted a red alert on every recording in the PoC.
var SESSION_STATES = STATES.concat(["pending"]);

// Persistent audio identity, shared by the bar, panel and saved sessions.
var GLYPH = "󱑽";             // U+F147D, Material Design waveform
var GLYPH_WORKING = "󰝲";     // loading, animated only during transcription
var GLYPH_STOP = "󰓛";        // stop square, for the Stop action
var GLYPH_DONE = "󰄬";         // check
var GLYPH_FAILED = "󰀪";       // alert
var GLYPH_FOLDER = "󰉋";       // folder, for the "open recordings" row

// `done` is shown for 30 s after the last transcript lands, then the widget
// goes back to hiding itself (spec 9.2).
var DONE_VISIBLE_SECONDS = 30;

// The daemon rewrites state.json every 30 s while it lives, for exactly one
// reason: so a reader can tell it apart from a file left behind by a daemon
// that was killed. Three missed heartbeats is the threshold. Without this a
// SIGKILL'd munin-rec leaves `state: "recording"` on disk and the bar shows a
// pulsing red dot and a climbing clock forever, for a meeting that stopped
// being recorded the instant the process died -- the worst thing a recording
// indicator can do (D10).
var STALE_AFTER_SECONDS = 90;

// The three shapes of spec section 6.3, in match order. Mirrors the
// [[detection.apps]] rows of contracts section 9, so the plugin behaves the
// same way `detect/linux.py` does. The daemon's config is authoritative and
// reaches the plugin as `detection_rules` in state.json; these are only the
// fallback for a daemon that has not written one. The plugin still does not
// read config.toml itself -- it has no TOML parser and no business owning the
// user's config -- which is why the daemon publishes the resolved table.
var DEFAULT_APP_RULES = [
    { app_id: "teams-native", label: "Microsoft Teams", client_name: "Teams",
      binary: "teams-for-linux" },
    { app_id: "teams-pwa", label: "Microsoft Teams",
      window_class: "chrome-teams.microsoft.com__-Default" },
    { app_id: "teams-tab", label: "Microsoft Teams",
      window_title_contains: "Microsoft Teams" }
];

// ------------------------------------------------------------- state.json

function emptyState() {
    return {
        schema_version: SCHEMA_VERSION,
        state: "idle",
        since: null,
        started_at: null,
        title: "",
        session: null,
        session_id: null,
        segment: 0,
        detected_app: null,
        grace_deadline: null,
        queue_depth: 0,
        last_error: null,
        idle_was_inhibited: false,
        detection_rules: [],
        // Which backend the worker would use. "none" is the PoC: nothing will
        // ever drain the queue, so the bar must not promise that it will.
        transcription_backend: null,
        // D15's window, so Resume can say how long it still means "the same
        // meeting". null when the daemon predates the field.
        resume_window_seconds: null,
        updated_at: null,
        daemon_pid: 0,
        // Not part of the daemon's contract: set by parseState so the widget
        // can tell "no daemon, or an unreadable file" from a daemon that is
        // genuinely idle.
        loaded: false
    };
}

// Returns the state view, or a safe idle view when the file is missing,
// truncated or half-written. The daemon writes atomically, but a reader must
// still never throw into the shell: a parse error here would take the whole
// bar down with it.
function parseState(text) {
    var view = emptyState();
    var raw = null;
    try {
        raw = JSON.parse(String(text || ""));
    } catch (e) {
        return view;
    }
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return view;

    view.loaded = true;
    view.schema_version = numberOr(raw.schema_version, SCHEMA_VERSION);
    view.state = STATES.indexOf(String(raw.state || "")) !== -1
        ? String(raw.state) : "idle";
    view.since = stringOrNull(raw.since);
    view.started_at = stringOrNull(raw.started_at);
    view.title = raw.title === null || raw.title === undefined ? "" : String(raw.title);
    view.session = stringOrNull(raw.session);
    view.session_id = stringOrNull(raw.session_id);
    view.segment = numberOr(raw.segment, 0);
    view.detected_app = parseDetectedApp(raw.detected_app);
    view.grace_deadline = stringOrNull(raw.grace_deadline);
    view.queue_depth = Math.max(0, numberOr(raw.queue_depth, 0));
    view.last_error = stringOrNull(raw.last_error);
    view.idle_was_inhibited = raw.idle_was_inhibited === true;
    view.detection_rules = parseRules(raw.detection_rules);
    view.transcription_backend = stringOrNull(raw.transcription_backend);
    view.resume_window_seconds = raw.resume_window_seconds === null
        || raw.resume_window_seconds === undefined
        ? null : Math.max(0, numberOr(raw.resume_window_seconds, 0));
    view.updated_at = stringOrNull(raw.updated_at);
    view.daemon_pid = numberOr(raw.daemon_pid, 0);
    return view;
}

// The daemon's [[detection.apps]] table, as published in state.json. Every
// field is coerced and a row missing an id or a label is dropped: this is a
// file, and a widget that throws on a malformed one takes the bar down with it.
function parseRules(raw) {
    var out = [];
    if (!Array.isArray(raw)) return out;
    var fields = ["client_name", "binary", "window_class", "window_title_contains"];
    for (var i = 0; i < raw.length; i++) {
        var row = raw[i];
        if (!row || typeof row !== "object") continue;
        var app_id = String(row.app_id || "");
        var label = String(row.label || "");
        if (app_id === "" || label === "") continue;
        var rule = { app_id: app_id, label: label };
        for (var f = 0; f < fields.length; f++)
            if (row[fields[f]]) rule[fields[f]] = String(row[fields[f]]);
        out.push(rule);
    }
    return out;
}

// The rules to match against: the daemon's if it published any, else the
// fallback compiled in above.
function rulesFor(view) {
    var rules = view && Array.isArray(view.detection_rules) ? view.detection_rules : [];
    return rules.length ? rules : DEFAULT_APP_RULES;
}

function parseDetectedApp(raw) {
    if (!raw || typeof raw !== "object") return null;
    return {
        app_id: String(raw.app_id || ""),
        label: String(raw.label || ""),
        pid: numberOr(raw.pid, 0)
    };
}

// A `done` view older than DONE_VISIBLE_SECONDS reads as idle. Doing the
// expiry here rather than with a timer that rewrites state.json keeps the
// rule "no timer ever writes this file" (contracts 7.2).
function effectiveState(view, nowMs) {
    if (!view) return "idle";
    var state = String(view.state || "idle");
    if (isStale(view, nowMs)) {
        // Nobody is behind this file any more. The live states become `failed`,
        // whose primary action is `munin start --resume` -- which is what the
        // user actually wants once the daemon is back and has recovered the
        // session. `done` still ages out; the settled states are still true.
        if (state === "recording" || state === "ending" || state === "detected")
            return "failed";
        if (state === "done") return "idle";
        return state;
    }
    if (state !== "done") return state;
    var age = elapsedSeconds(view.since || view.updated_at, nowMs);
    return age >= 0 && age > DONE_VISIBLE_SECONDS ? "idle" : "done";
}

// Has the daemon missed enough heartbeats that this file cannot be believed?
// A file that was never loaded is not stale, it is absent -- a different thing,
// and the widget hides itself for it either way.
function isStale(view, nowMs) {
    if (!view || view.loaded !== true) return false;
    var age = elapsedSeconds(view.updated_at, nowMs);
    return age >= 0 && age > STALE_AFTER_SECONDS;
}

// ---------------------------------------------------------------- time

// ISO 8601 with a local offset, second precision. Returns NaN for anything
// this cannot read, and callers treat NaN as "unknown", never as zero.
function parseIso(text) {
    var s = String(text || "").trim();
    if (!s) return NaN;
    var ms = Date.parse(s);
    return typeof ms === "number" && !isNaN(ms) ? ms : NaN;
}

// startedAt is ISO 8601 with a local offset. The plugin computes elapsed time
// itself, so no timer ever writes state.json. Returns -1 when startedAt is
// unreadable, and clamps at 0 rather than counting backwards over a clock
// step.
function elapsedSeconds(startedAt, nowMs) {
    var started = parseIso(startedAt);
    if (isNaN(started)) return -1;
    var now = typeof nowMs === "number" && !isNaN(nowMs) ? nowMs : Date.now();
    return Math.max(0, Math.floor((now - started) / 1000));
}

// "HH:MM:SS", hours zero-padded to two but not bounded by 24: a recording
// that ran overnight reads 26:04:11, not 02:04:11.
function formatElapsed(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    return pad2(h) + ":" + pad2(m) + ":" + pad2(s);
}

// "MM:SS" under an hour, "H:MM:SS" above it. Used where the surrounding text
// already says what the number is (the panel hero), so leading zeros are
// noise rather than alignment.
function formatElapsedShort(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    return h > 0 ? h + ":" + pad2(m) + ":" + pad2(s) : pad2(m) + ":" + pad2(s);
}

// A duration in words, for a finished session: "52 min", "1 h 14 min".
// Metric units and 24-hour time throughout, per house style.
function formatDuration(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    if (total < 60) return total + " s";
    var h = Math.floor(total / 3600);
    var m = Math.round((total % 3600) / 60);
    if (m === 60) { h += 1; m = 0; }
    if (h === 0) return m + " min";
    return m === 0 ? h + " h" : h + " h " + m + " min";
}

// "13:25" in the viewer's own zone. The offset in the timestamp is what the
// daemon recorded; Date renders it locally, which is what a person reading
// their own bar expects.
function formatClock(iso) {
    var ms = parseIso(iso);
    if (isNaN(ms)) return "";
    var d = new Date(ms);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

function pad2(n) {
    var v = Math.floor(Math.abs(Number(n) || 0));
    return (v < 10 ? "0" : "") + v;
}

// ---------------------------------------------------------------- bar

// Keep Munin available as the entry point to recordings, including while idle.
function visible(state) {
    return true;
}

// A queue nothing will drain. With `transcription_backend: "none"` a captured
// session is the designed end state, not work in progress, so the bar shows a
// static waveform and no count: the count lives in the panel, where the reason
// is printed next to it. A spinner or a permanent "1 queued" would claim that
// something is happening.
function deferred(view, nowMs) {
    if (!view || view.transcription_backend !== "none") return false;
    var s = effectiveState(view, nowMs);
    return s === "captured" || s === "transcribing";
}

function barGlyphFor(view, nowMs) {
    if (deferred(view, nowMs)) return GLYPH;
    return barGlyph(effectiveState(view, nowMs));
}

function barToneFor(view, nowMs) {
    if (deferred(view, nowMs)) return "dim";
    return barTone(effectiveState(view, nowMs));
}

function barGlyph(state) {
    switch (String(state || "idle")) {
    case "idle":
    case "detected":
    case "captured": return GLYPH;
    case "transcribing": return GLYPH_WORKING;
    case "done": return GLYPH_DONE;
    case "failed": return GLYPH_FAILED;
    default: return "";     // recording and ending render a dot, not a glyph
    }
}

// recording, ending and failed are the states that may not be missed, so they
// take the theme's urgent colour. detected deliberately reads quiet: it is
// honest about doing nothing.
function barTone(state) {
    switch (String(state || "idle")) {
    case "recording":
    case "ending":
    case "failed": return "urgent";
    case "detected": return "dim";
    default: return "foreground";
    }
}

// Only `recording` pulses. `ending` is the same red held steady, and that is
// the whole difference the user has to read: still capturing, but on a timer.
function barPulses(state) {
    return String(state || "idle") === "recording";
}

function barShowsDot(state) {
    var s = String(state || "idle");
    return s === "recording" || s === "ending";
}

// Only `transcribing` spins, because only `transcribing` is work in progress.
// `captured` means the audio is safe and the worker has not picked it up -- in
// the PoC, where the backend is `none`, that is the *designed* end state and it
// persists indefinitely. A spinner there is an infinite animation repainting the
// bar forever, claiming work that nothing is doing.
function barSpinsFor(view, nowMs) {
    return !deferred(view, nowMs) && barSpins(effectiveState(view, nowMs));
}

function barSpins(state) {
    return String(state || "idle") === "transcribing";
}

function barLabel(view, nowMs) {
    if (!view) return "";
    if (deferred(view, nowMs)) return "";
    var state = effectiveState(view, nowMs);
    var secs;
    switch (state) {
    case "detected":
        return view.detected_app && view.detected_app.label
            ? view.detected_app.label : "Call detected";
    case "recording":
    case "ending":
        secs = elapsedSeconds(view.started_at, nowMs);
        return secs < 0 ? "" : formatElapsed(secs);
    case "captured":
    case "transcribing":
        return view.queue_depth > 0 ? view.queue_depth + " queued" : "Queued";
    case "done":
        return "Transcript ready";
    case "failed":
        return "Retry";
    default:
        return "";
    }
}

function tooltipText(view, nowMs) {
    if (!view) return "Munin";
    if (isStale(view, nowMs) && STATES.indexOf(String(view.state)) > 0) {
        return "munin-rec has stopped answering. Anything it was recording "
            + "ended when it did; the audio already written is kept.";
    }
    var state = effectiveState(view, nowMs);
    var title = view.title ? view.title : "Untitled session";
    var who;
    switch (state) {
    case "detected":
        who = view.detected_app && view.detected_app.label
            ? view.detected_app.label : "A call";
        return who + " is live. Click to record.";
    case "recording":
        return "Recording " + title;
    case "ending":
        return "Meeting seems over: " + title + ". Recording stops soon.";
    case "captured":
    case "transcribing":
        if (deferred(view, nowMs)) {
            return view.queue_depth + " captured and safe on disk, waiting: "
                + "no transcription backend is configured yet.";
        }
        return "Captured and safe on disk. " + view.queue_depth + " in the queue.";
    case "done":
        return "Transcript ready: " + title;
    case "failed":
        return view.last_error ? "Failed: " + view.last_error
            : "Failed. The audio is kept.";
    default:
        return "Munin";
    }
}

// The one button the panel puts first. `argv` is a CLI invocation, never a
// socket call: the plugin has exactly one way to ask for something (D20, and
// contracts 7.2 -- one client of the wire protocol).
function primaryAction(state) {
    switch (String(state || "idle")) {
    case "recording":
    case "ending":
        return { label: "Stop and transcribe", argv: ["munin", "stop"] };
    case "captured":
    case "done":
    case "failed":
        return { label: "Resume", argv: ["munin", "start", "--resume"] };
    case "detected":
        return { label: "Record", argv: ["munin", "start", "--from-detection"] };
    default:
        return { label: "Record", argv: ["munin", "start"] };
    }
}

// The one action that cannot live on a notification. `omarchy-notification-send`
// takes exactly one `--exec`, and the "streams gone" notification spends it on
// "Stop and transcribe" (contracts 8) -- so *Keep recording* has to be here.
// `munin event call-started` is what cancels the grace period; `start --resume`
// is not it, because the session is still recording.
function secondaryAction(state) {
    if (String(state || "idle") === "ending")
        return { label: "Keep recording", argv: ["munin", "event", "call-started"] };
    return null;
}

// Seconds of D15's resume window still open after the last capture, or -1
// when the question does not apply (not a finished session, no window
// published, no `since`). `since` is the moment the daemon wrote the finished
// state, which is when the window starts.
function resumeSecondsLeft(view, nowMs) {
    if (!view) return -1;
    var state = effectiveState(view, nowMs);
    if (state !== "captured" && state !== "done" && state !== "failed") return -1;
    if (view.resume_window_seconds === null || view.resume_window_seconds === undefined) return -1;
    var elapsed = elapsedSeconds(view.since, nowMs);
    if (elapsed < 0) return -1;
    return Math.max(0, view.resume_window_seconds - elapsed);
}

function formatLeft(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    if (seconds >= 60) return Math.ceil(seconds / 60) + " min left";
    return seconds + " s left";
}

// The two panel buttons for a finished session, which is the state the panel
// is most often opened in. Inside the window: Resume (labelled with what is
// left of it) and New recording side by side, because `munin start --resume`
// continues the previous meeting and a click meant as "new" must have its
// own button. Past the window Resume would silently start a fresh session
// anyway, so the button says what it does.
function primaryActionFor(view, nowMs) {
    var left = resumeSecondsLeft(view, nowMs);
    if (left > 0) {
        return { label: "Resume · " + formatLeft(left), argv: ["munin", "start", "--resume"] };
    }
    if (left === 0) return { label: "New recording", argv: ["munin", "start"] };
    return primaryAction(effectiveState(view, nowMs));
}

function secondaryActionFor(view, nowMs) {
    var left = resumeSecondsLeft(view, nowMs);
    if (left > 0) return { label: "New recording", argv: ["munin", "start"] };
    return secondaryAction(effectiveState(view, nowMs));
}

// Wrap an argv vector in a login shell without letting it be re-tokenized:
// `exec "$@"` puts every element in a positional parameter, so a window title
// carrying $(...), a quote or a newline stays literal. The login shell is
// what puts ~/.local/bin -- where `munin` lives -- on PATH for a process
// spawned by the bar. Same idiom as qs.Commons' Util.execArgv.
function muninArgv(argv) {
    var list = Array.isArray(argv) ? argv : [];
    var out = ["bash", "-lc", 'exec "$@"', "bash"];
    for (var i = 0; i < list.length; i++) out.push(String(list[i]));
    return out;
}

// ---------------------------------------------------------------- sessions

// Parses `munin list --json`, which is {"sessions": [...]}. Anything that is
// not that shape yields an empty list rather than an exception.
function parseSessions(text) {
    var raw = null;
    try {
        raw = JSON.parse(String(text || ""));
    } catch (e) {
        return [];
    }
    var list = raw && Array.isArray(raw.sessions) ? raw.sessions
        : (Array.isArray(raw) ? raw : []);
    var out = [];
    for (var i = 0; i < list.length; i++) {
        var s = list[i];
        if (!s || typeof s !== "object") continue;
        out.push({
            id: String(s.id || ""),
            state: SESSION_STATES.indexOf(String(s.state || "")) !== -1
                ? String(s.state) : "unknown",
            title: s.title ? String(s.title) : String(s.id || "Untitled"),
            started_at: stringOrNull(s.started_at),
            duration_seconds: numberOr(s.duration_seconds, 0),
            path: stringOrNull(s.path),
            pending_reason: stringOrNull(s.pending_reason)
        });
    }
    return out;
}

function sessionGlyph(state) {
    switch (String(state || "")) {
    case "recording":
    case "ending": return GLYPH;
    case "done": return GLYPH_DONE;
    case "failed":
    case "unknown": return GLYPH_FAILED;
    case "transcribing": return GLYPH_WORKING;
    default: return GLYPH;    // captured, pending: audio saved
    }
}

// The second line of a session row. A pending session says why it is pending:
// in the PoC every captured session lands there, and a row that only said
// "pending" would read as a bug rather than a decision.
function sessionMeta(session) {
    if (!session) return "";
    var parts = [];
    var clock = formatClock(session.started_at);
    if (clock) parts.push(clock);
    if (session.duration_seconds > 0) parts.push(formatDuration(session.duration_seconds));
    if (session.pending_reason) parts.push(session.pending_reason);
    else if (session.state === "failed") parts.push("audio kept");
    return parts.join(" · ");
}

// ---------------------------------------------------------------- detection

// Condition 2 of spec 6.3, in match order: the PipeWire client first (the
// native app is unambiguous and costs nothing), then the window class, then
// the window title. Same three-shape table as `detect/base.identify`, so the
// plugin and the daemon-side detector agree on what Teams is.
//
// Rules are tried in file order within each stage, which is why the loop runs
// three times over the whole list rather than once per rule.
function identify(clientName, windowClass, windowTitle, rules, binary) {
    var list = Array.isArray(rules) && rules.length ? rules : DEFAULT_APP_RULES;
    var client = String(clientName || "").toLowerCase();
    var bin = String(binary || "").toLowerCase();
    var cls = String(windowClass || "").toLowerCase();
    var title = String(windowTitle || "").toLowerCase();
    var i, rule;

    // Every comparison is case-folded and the binary is a match shape of its
    // own, because `detect/base.identify` does both: a rule that fires in the
    // daemon and misses here would make detection depend on which source the
    // config happens to name, which is the opposite of one match table.
    for (i = 0; i < list.length; i++) {
        rule = list[i];
        if (!rule) continue;
        if (rule.client_name && client && String(rule.client_name).toLowerCase() === client)
            return { app_id: rule.app_id, label: rule.label, matched_by: "pipewire" };
        if (rule.binary && bin && String(rule.binary).toLowerCase() === bin)
            return { app_id: rule.app_id, label: rule.label, matched_by: "pipewire" };
    }
    for (i = 0; i < list.length; i++) {
        rule = list[i];
        if (rule && rule.window_class && cls
            && String(rule.window_class).toLowerCase() === cls)
            return { app_id: rule.app_id, label: rule.label, matched_by: "window_class" };
    }
    for (i = 0; i < list.length; i++) {
        rule = list[i];
        if (rule && rule.window_title_contains && title
            && title.indexOf(String(rule.window_title_contains).toLowerCase()) !== -1)
            return { app_id: rule.app_id, label: rule.label, matched_by: "window_title" };
    }
    return null;
}

// Condition 1 of spec 6.3: one owner holding a playback stream and a capture
// stream at the same time. `owners` is a map of owner key to
// { pid, client_name, binary, playback, capture, playback_handle } as
// Service.qml accumulates it from the PipeWire node list.
//
// The owner key is the pid where PipeWire published one on the node, and the
// PipeWire client id otherwise: some clients publish process identity on the
// Client object rather than the node, and Quickshell exposes nodes only.
// Grouping by client id keeps condition 1 correct even when the pid is
// unknown; the pid is then simply not passed to the daemon.
function liveCalls(owners) {
    var out = [];
    if (!owners || typeof owners !== "object") return out;
    for (var key in owners) {
        if (!Object.prototype.hasOwnProperty.call(owners, key)) continue;
        var o = owners[key];
        if (!o || !o.playback || !o.capture) continue;
        out.push({
            key: String(key),
            pid: numberOr(o.pid, 0),
            client_name: o.client_name ? String(o.client_name) : "",
            binary: o.binary ? String(o.binary) : "",
            playback_handle: o.playback_handle ? String(o.playback_handle) : ""
        });
    }
    out.sort(function (a, b) { return a.key < b.key ? -1 : (a.key > b.key ? 1 : 0); });
    return out;
}

// What changed between two scans. Returns the calls to report as started and
// the calls to report as ended, so Service.qml only runs the CLI on a real
// transition rather than once per PipeWire event.
function diffCalls(previous, current) {
    var before = keySet(previous);
    var after = keySet(current);
    var started = [];
    var ended = [];
    var cur = Array.isArray(current) ? current : [];
    var prev = Array.isArray(previous) ? previous : [];
    var i;
    for (i = 0; i < cur.length; i++)
        if (!before[cur[i].key]) started.push(cur[i]);
    for (i = 0; i < prev.length; i++)
        if (!after[prev[i].key]) ended.push(prev[i]);
    return { started: started, ended: ended };
}

function keySet(calls) {
    var set = {};
    var list = Array.isArray(calls) ? calls : [];
    for (var i = 0; i < list.length; i++) if (list[i]) set[String(list[i].key)] = true;
    return set;
}

// The argv for a detection event. `title` is a window title straight off the
// compositor, so it is data and never part of a command string: see
// muninArgv(). A pid of 0 means PipeWire did not publish one, and the flag is
// then left off rather than sent as a lie.
function callEventArgv(event, call, identity, windowTitle) {
    var argv = ["munin", "event", String(event)];
    var started = String(event) === "call-started";
    if (call && numberOr(call.pid, 0) > 0) argv.push("--pid", String(Math.floor(call.pid)));
    // `--app` is the human label and `--app-id` the rule id: the daemon writes
    // both into session.json and the notification. Sending the rule id as the
    // label made every recording's provenance read "teams-pwa / unknown".
    if (identity && identity.label) argv.push("--app", String(identity.label));
    if (identity && identity.app_id) argv.push("--app-id", String(identity.app_id));
    // The playback handle is the whole point of the event. Without it the
    // daemon has no app target, and the capturer writes a *silent* app track --
    // a two-track recorder producing one track of meeting audio and one of
    // nothing, unrecoverably, on the default configuration.
    if (started && call && call.playback_handle)
        argv.push("--handle", String(call.playback_handle));
    var title = String(windowTitle || "").trim();
    if (title && started) argv.push("--title", title);
    return argv;
}

// ---------------------------------------------------------------- helpers

function numberOr(value, fallback) {
    var n = Number(value);
    return typeof n === "number" && !isNaN(n) && isFinite(n) ? n : fallback;
}

function stringOrNull(value) {
    if (value === null || value === undefined) return null;
    var s = String(value);
    return s === "" ? null : s;
}
