// Pure state helpers for the Munin plugin: parsing state.json, formatting
// elapsed time, and mapping a state to a glyph and colour.
//
// Kept out of the QML so the same logic is readable in one place and has no
// dependency on the shell's object graph.
//
// Owner: plugin workstream. Contract:
// docs/superpowers/specs/2026-09-14-poc-contracts.md, sections 7.2 and 8.

.pragma library

// Bar states, in the order of spec section 9.2.
var STATES = ["idle", "detected", "recording", "ending",
              "captured", "transcribing", "done", "failed"];

// U+F0EC2, the glyph bar/indicators/ScreenRecording.qml uses.
var GLYPH = "2";

function parseState(text) {
    // Returns the state view, or a safe idle view when the file is missing,
    // truncated or half-written. The daemon writes atomically, but a reader
    // must still never throw into the shell.
    throw "not implemented";
}

function elapsedSeconds(startedAt, now) {
    // startedAt is ISO 8601 with a local offset. The plugin computes elapsed
    // time itself, so no timer ever writes state.json.
    throw "not implemented";
}

function formatElapsed(seconds) {
    // "MM:SS" under an hour, "H:MM:SS" above it.
    throw "not implemented";
}

function visible(state) {
    // idle hides the widget entirely (spec 9.2).
    throw "not implemented";
}
