// Munin plugin service: watches $XDG_RUNTIME_DIR/munin/state.json and, when
// configured as the detection source, watches Quickshell.Services.Pipewire for
// a process holding a playback and a capture stream at once.
//
// The plugin never captures (D20). It detects, prompts and renders; munin-rec
// captures. A shell hot-reload must never kill a recording, which is also why
// this service only ever runs `munin ...` through execDetached and never opens
// the daemon socket.
//
// Owner: plugin workstream.

import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Services.Pipewire

Item {
    id: service

    // --- state.json, the daemon's plugin-facing view -------------------
    readonly property string statePath:
        (Quickshell.env("XDG_RUNTIME_DIR") || "") + "/munin/state.json"

    property var status: ({ state: "idle" })

    FileView {
        id: stateFile
        path: service.statePath
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            // TODO(plugin workstream): Model.parseState(stateFile.text())
        }
    }

    // --- detection, condition 1 of spec 6.3 ----------------------------
    // One process holding both a playback and a capture stream. Evidence is
    // reported to the daemon with `munin event call-started`; the daemon owns
    // the state machine and every timer.
    function reportCallStarted(pid, app, title) {
        // TODO(plugin workstream): Quickshell.execDetached([...])
    }

    function reportCallEnded(pid) {
        // TODO(plugin workstream)
    }
}
