// Munin plugin service.
//
// Two jobs, and nothing else:
//
//   1. Read $XDG_RUNTIME_DIR/munin/state.json -- the daemon's only
//      plugin-facing output -- and publish it as properties the bar widget
//      binds to. The file is watched; a 2 s timer re-reads it as well,
//      because the watch cannot attach to a file that does not exist yet and
//      the daemon creates it on start.
//
//   2. Watch Quickshell.Services.Pipewire for condition 1 of spec 6.3 (one
//      process holding a playback stream and a capture stream at the same
//      time), attach condition 2 (is it Teams) from the PipeWire client name
//      or the Hyprland window owning that pid, and tell the daemon about the
//      transition by running `munin event call-started|call-ended`.
//
// The plugin never captures (D20) and never opens the daemon socket
// (contracts 7.2). A shell hot-reload or crash must not be able to kill a
// recording, so everything here is advisory: the daemon owns the state
// machine, every timer that matters, and the decision to prompt. If this
// service dies mid-meeting, munin-rec keeps recording and the CLI and the
// keybind still work.
//
// Detection events are only sent while a daemon is actually running
// (state.json present, daemon_pid set), so a stopped daemon never causes a
// process spawn per PipeWire event. The daemon ignores plugin events when
// `detection.source` is not "plugin" -- that is what the `accepted` field of
// the `event` reply is for.
//
// Owner: plugin workstream.

import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import Quickshell.Services.Pipewire
import "Model.js" as Model

Item {
    id: service

    // Injected by the host shell when the plugin is loaded (shell.qml
    // ensureService). Unused so far -- kept because the injection is the
    // documented contract and a later milestone will want summon().
    property var shell: null
    property var manifest: null

    // --- state.json, the daemon's plugin-facing view -------------------

    readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") || ""
    readonly property string statePath: runtimeDir === ""
        ? "" : runtimeDir + "/munin/state.json"

    // The parsed state view of contracts 7.2. Always a full object, never
    // null: the widget binds straight into it.
    property var status: Model.emptyState()

    // Named muninState, not `state`: Item already has a `state` property and
    // shadowing it would make every QML reader wonder which one they have.
    readonly property string muninState: status ? String(status.state) : "idle"
    // `loaded` and a pid are not enough: state.json outlives a SIGKILL'd daemon,
    // pid and all. The 30 s heartbeat is the liveness signal, and Model.isStale
    // is what reads it. Without this the plugin keeps spawning `munin event`
    // at a socket nobody is listening on, and the bar keeps counting a
    // recording that ended when the process did.
    readonly property bool daemonRunning: !!status && status.loaded === true
        && status.daemon_pid > 0 && !Model.isStale(status, Date.now())

    function applyState(text) {
        service.status = Model.parseState(text);
    }

    function refreshState() {
        stateFile.reload();
    }

    FileView {
        id: stateFile
        path: service.statePath
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: service.applyState(stateFile.text())
        // A missing file is the normal state before the daemon has ever run.
        onLoadFailed: service.applyState("")
    }

    // The watch cannot attach to a path that does not exist, and the daemon
    // creates state.json on start. Two seconds is slow enough to cost
    // nothing and fast enough that the bar is never more than one blink
    // behind a daemon that just came up.
    Timer {
        interval: 2000
        repeat: true
        running: true
        onTriggered: stateFile.reload()
    }

    // --- detection, condition 1 of spec 6.3 ----------------------------

    // Every PipeWire *audio* stream node, sinks and sources alike. Streams only:
    // devices are not what a call is made of -- and neither is video. A
    // Stream/Input/Video node (a screencast through xdg-desktop-portal, or a
    // webcam) publishes isStream true and isSink false, so classifying on
    // isSink alone would book it as a capture stream and let "Chrome plays a
    // video in one tab while sharing its screen" satisfy condition 1. That is
    // precisely the false positive spec 6.3's second condition exists to
    // remove. `audio` is null on a non-audio node, which is the same gate
    // panels/audio/Panel.qml uses on its own stream list.
    readonly property var streamNodes: {
        var out = [];
        var nodes = Pipewire.nodes ? Pipewire.nodes.values : [];
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            if (n && n.isStream && n.audio) out.push(n);
        }
        return out;
    }

    // PwNode.properties is invalid until the node is bound, and the audio
    // panel warns that reading it on an unbound node while capture streams
    // are appearing can destabilize Quickshell's PipeWire service. Tracking
    // the nodes is what makes `ready` and `properties` safe to read at all.
    PwObjectTracker {
        objects: service.streamNodes
    }

    // The calls currently live, as Model.liveCalls returns them. Written by
    // rescan(), never bound, because it must change on a debounce rather
    // than on every PipeWire event.
    property var liveCalls: []

    // What was last reported to the daemon. The difference between this and
    // liveCalls is the only thing that ever spawns a process.
    property var reportedCalls: []

    // A stream list churns hard at the start and end of a call -- Chrome
    // alone adds and removes several nodes over a second or two. Debouncing
    // means one `munin event` per transition rather than one per node.
    Timer {
        id: debounce
        interval: 1200
        repeat: false
        onTriggered: service.rescan()
    }

    onStreamNodesChanged: debounce.restart()

    Connections {
        target: Pipewire
        function onReadyChanged() { debounce.restart() }
    }

    function nodeProps(node) {
        // `ready` is the gate the first-party code uses; without it
        // `properties` is not merely empty, it is invalid.
        return node && node.ready && node.properties ? node.properties : ({});
    }

    // Group every stream node by the process that owns it. PipeWire publishes
    // process identity on the Client object rather than the node for some
    // clients, and Quickshell exposes nodes only -- so fall back to the
    // client id as the grouping key and report no pid at all rather than a
    // guessed one.
    function ownersFromNodes() {
        var owners = ({});
        var nodes = service.streamNodes;
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            if (!n || !n.ready || !n.audio) continue;
            var p = nodeProps(n);

            var pid = Number(p["application.process.id"] || 0);
            if (isNaN(pid) || pid < 0) pid = 0;
            var clientId = String(p["client.id"] || "");
            var key = pid > 0 ? "pid:" + pid
                : (clientId !== "" ? "client:" + clientId : "node:" + n.id);

            var owner = owners[key];
            if (!owner) {
                owner = {
                    pid: pid,
                    client_name: String(p["application.name"] || n.description || ""),
                    binary: String(p["application.process.binary"] || ""),
                    playback: false,
                    capture: false,
                    playback_handle: ""
                };
                owners[key] = owner;
            }
            if (!owner.client_name) owner.client_name = String(p["application.name"] || "");
            if (!owner.binary) owner.binary = String(p["application.process.binary"] || "");

            // Quickshell versions differ in how `type` is exposed, so use the
            // flags the audio panel relies on instead: a playback stream
            // publishes isSink true, a capture stream publishes as a stream
            // source.
            if (n.isSink === true) {
                owner.playback = true;
                // `object.serial` and nothing else: contracts section 6 fixes
                // the handle as the output node's serial, which is what
                // `pw-record --target` takes. A node id is a different
                // namespace -- handing one over binds the wrong node or none.
                if (!owner.playback_handle)
                    owner.playback_handle = String(p["object.serial"] || "");
            } else {
                owner.capture = true;
            }
        }
        return owners;
    }

    // --- condition 2 of spec 6.3: the window owning the pid -------------

    // Quickshell's Hyprland models are empty until the IPC singleton has been
    // touched and its event socket has connected. Verified on this machine: a
    // read at load returned 0 toplevels and a read a few seconds later
    // returned the real window list. Binding the count here makes that
    // connection happen when the plugin loads rather than when the first call
    // starts, so the first detection already has a window to identify itself
    // by.
    readonly property int toplevelCount:
        Hyprland.toplevels ? Hyprland.toplevels.values.length : 0

    function toplevelForPid(pid) {
        if (!pid || pid <= 0) return null;
        var list = Hyprland.toplevels ? Hyprland.toplevels.values : [];
        for (var i = 0; i < list.length; i++) {
            var t = list[i];
            var ipc = t && t.lastIpcObject ? t.lastIpcObject : null;
            if (ipc && Number(ipc.pid) === Number(pid)) return ipc;
        }
        return null;
    }

    function windowClassOf(ipc) {
        if (!ipc) return "";
        return String(ipc["class"] || ipc.initialClass || "");
    }

    function windowTitleOf(ipc) {
        if (!ipc) return "";
        return String(ipc.title || ipc.initialTitle || "");
    }

    // --- the /proc parent walk ------------------------------------------
    //
    // Chrome's audio process is not the process that owns the window: the
    // window pid was observed one hop above it. Only one hop was ever seen,
    // so the walk is bounded at MAX_PARENT_HOPS rather than assumed to be a
    // single step, and it gives up quietly rather than failing.
    //
    // This is asynchronous on purpose. A blocking read of /proc from the bar
    // would be small but it would be on the shell's main thread, and this
    // runs while a meeting is starting.

    readonly property int maxParentHops: 5

    property var resolveQueue: []
    property var resolveCurrent: null
    property int resolveHops: 0
    property int resolvePid: 0

    function queueResolve(call) {
        var q = service.resolveQueue.slice();
        q.push(call);
        service.resolveQueue = q;
        if (!service.resolveCurrent) nextResolve();
    }

    function nextResolve() {
        if (service.resolveQueue.length === 0) {
            service.resolveCurrent = null;
            return;
        }
        var q = service.resolveQueue.slice();
        var call = q.shift();
        service.resolveQueue = q;
        service.resolveCurrent = call;
        service.resolveHops = 0;
        service.resolvePid = Number(call.pid) || 0;

        var ipc = toplevelForPid(service.resolvePid);
        if (ipc || service.resolvePid <= 0) {
            finishResolve(ipc);
            return;
        }
        procFile.path = "/proc/" + service.resolvePid + "/status";
        procFile.reload();
    }

    function stepResolve(text) {
        var ppid = parsePpid(text);
        service.resolveHops += 1;
        if (ppid <= 0 || service.resolveHops >= service.maxParentHops) {
            finishResolve(null);
            return;
        }
        var ipc = toplevelForPid(ppid);
        if (ipc) {
            finishResolve(ipc);
            return;
        }
        service.resolvePid = ppid;
        procFile.path = "/proc/" + ppid + "/status";
        procFile.reload();
    }

    function parsePpid(text) {
        var lines = String(text || "").split("\n");
        for (var i = 0; i < lines.length; i++) {
            if (lines[i].indexOf("PPid:") !== 0) continue;
            var n = parseInt(lines[i].substring(5).replace(/\s+/g, ""), 10);
            return isNaN(n) ? 0 : n;
        }
        return 0;
    }

    function finishResolve(ipc) {
        var call = service.resolveCurrent;
        service.resolveCurrent = null;
        if (call) {
            // The daemon's own `[[detection.apps]]` table, published in
            // state.json, with the plugin's compiled-in copy only as a fallback
            // for a daemon too old to send it (D13: another application is a
            // row in config.toml, never code).
            var identity = Model.identify(call.client_name, windowClassOf(ipc),
                                          windowTitleOf(ipc),
                                          Model.rulesFor(service.status),
                                          call.binary);
            // Both conditions of spec 6.3, not just the first. An unidentified
            // owner holding a playback and a capture stream is a Discord call,
            // a Signal call, a WebRTC page -- conversations Munin was never
            // asked to care about, whose participants saw no prompt. Reporting
            // one would raise the D4 "record this?" notification for it.
            if (identity)
                send(Model.callEventArgv("call-started", call, identity,
                                         windowTitleOf(ipc)));
        }
        nextResolve();
    }

    FileView {
        id: procFile
        watchChanges: false
        printErrors: false
        onLoaded: service.stepResolve(procFile.text())
        // A process that exited between the scan and the read is ordinary,
        // not an error: give up on this call and take the next one.
        onLoadFailed: service.finishResolve(null)
    }

    // --- the scan -------------------------------------------------------

    function rescan() {
        var calls = Model.liveCalls(ownersFromNodes());
        var diff = Model.diffCalls(service.liveCalls, calls);
        service.liveCalls = calls;

        if (!service.daemonRunning) {
            // Nothing to tell, and nothing to spawn. Keep `reportedCalls` in
            // step so the daemon is not told about a call that started while
            // it was down as though it had just begun -- it will be, on the
            // next transition, which is the honest moment.
            service.reportedCalls = calls;
            return;
        }

        var i;
        for (i = 0; i < diff.started.length; i++) queueResolve(diff.started[i]);
        for (i = 0; i < diff.ended.length; i++)
            send(Model.callEventArgv("call-ended", diff.ended[i], null, ""));
        service.reportedCalls = calls;
    }

    // Everything this plugin asks for goes through the CLI, as one argv
    // vector that bash never re-tokenizes. A window title is attacker-ish
    // data by definition -- it is whatever the other end of the meeting
    // named the room.
    function send(argv) {
        Quickshell.execDetached(Model.muninArgv(argv));
    }

    // Kept for the bar widget's "Keep recording" and for anything that wants to
    // drive the detection path by hand. `handle` is optional but matters: with
    // no playback handle the daemon has no app target and the capturer writes a
    // silent app track, so pass the call's handle whenever one is known.
    function reportCallStarted(pid, app, title, handle, appId) {
        send(Model.callEventArgv(
            "call-started",
            { pid: pid, playback_handle: handle || "" },
            { app_id: appId || app, label: app },
            title));
    }

    function reportCallEnded(pid) {
        send(Model.callEventArgv("call-ended", { pid: pid }, null, ""));
    }

    // --- levels, for the panel's track meters ---------------------------
    //
    // Read straight off PipeWire, and only while the panel asks for them.
    // These are what the microphone and the call are doing right now, not
    // what munin-rec wrote to disk -- the plugin has no access to the
    // capture at all (D20). The panel says so.

    property bool metersEnabled: false

    readonly property var micNode: Pipewire.defaultAudioSource

    readonly property var appNode: {
        if (!service.metersEnabled) return null;
        var handle = "";
        for (var i = 0; i < service.liveCalls.length; i++)
            if (service.liveCalls[i].playback_handle) {
                handle = service.liveCalls[i].playback_handle;
                break;
            }
        if (handle === "") return null;
        var nodes = service.streamNodes;
        for (var j = 0; j < nodes.length; j++) {
            var n = nodes[j];
            if (!n || !n.ready) continue;
            var p = nodeProps(n);
            if (String(p["object.serial"] || n.id || "") === handle) return n;
        }
        return null;
    }

    PwObjectTracker {
        objects: service.micNode ? [service.micNode] : []
    }

    PwNodePeakMonitor {
        id: micPeak
        node: service.metersEnabled ? service.micNode : null
        enabled: service.metersEnabled && service.micNode !== null
    }

    PwNodePeakMonitor {
        id: appPeak
        node: service.metersEnabled ? service.appNode : null
        enabled: service.metersEnabled && service.appNode !== null
    }

    readonly property real micLevel: micPeak.enabled ? micPeak.peak : 0
    readonly property real appLevel: appPeak.enabled ? appPeak.peak : 0

    // --- IPC, for debugging from a terminal -----------------------------
    //
    //   omarchy-shell munin status
    //   omarchy-shell munin rescan
    //
    // Read-only, and deliberately not a control surface: starting and
    // stopping is the CLI's job, so there is exactly one path into the
    // daemon.

    IpcHandler {
        target: "munin"

        function status(): string {
            return JSON.stringify(service.status);
        }

        function calls(): string {
            return JSON.stringify(service.liveCalls);
        }

        function rescan(): string {
            service.rescan();
            return "ok";
        }
    }

    Component.onCompleted: {
        stateFile.reload();
        debounce.restart();
    }
}
