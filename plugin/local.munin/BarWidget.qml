// Munin bar widget and its dropdown panel.
//
// Built on qs.Ui's `Panel`, which is the idiom the first-party plugins use
// when one entry point owns both a bar button and a popup (panels/dropbox is
// the closest example). `Panel` gives the open/close lifecycle and the shape
// the bar's popout coordinator expects; `KeyboardPanel` gives the layer-shell
// popup, mouse dismissal and keyboard focus.
//
// The widget renders exactly one of the seven states of spec 9.2, and which
// one is a pure function of the daemon's state.json -- see Model.js. Nothing
// here decides anything:
//
//   idle          hidden
//   detected      dim microphone glyph and the app label
//   recording     pulsing red dot and elapsed time
//   ending        the same red dot held steady, still counting
//   transcribing  a turning glyph and the queue depth
//   done          a tick, for 30 s
//   failed        an exclamation, until it is acknowledged
//
// Actions run the CLI (`munin start|stop|toggle`). The widget never opens the
// daemon socket and never captures (D20, contracts 7.2). A shell reload
// therefore cannot hold the daemon's socket open, and cannot kill a
// recording.
//
// Owner: plugin workstream.

import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "components"

Panel {
    id: root

    moduleName: "local.munin"

    // The plugin's own service, looked up through the capability-scoped shell
    // facade the bar injects. A plugin may reach its own id and no other, so
    // this is the sanctioned way for a widget to read its service's state.
    readonly property var muninService: {
        var s = bar && bar.shell ? bar.shell : null;
        if (!s) return null;
        if (typeof s.serviceFor === "function") {
            var own = s.serviceFor(root.moduleName);
            if (own) return own;
        }
        if (typeof s.firstPartyServiceFor === "function")
            return s.firstPartyServiceFor(root.moduleName);
        return null;
    }

    readonly property var status: muninService && muninService.status
        ? muninService.status : Model.emptyState()

    // The clock the widget counts with. Nothing writes state.json on a timer
    // (contracts 7.2): the daemon records started_at once and the bar does
    // the arithmetic. The tick stops the moment nothing is counting.
    property double nowMs: Date.now()

    readonly property string barState: Model.effectiveState(status, nowMs)
    readonly property bool counting: barState === "recording" || barState === "ending"
    readonly property string barLabel: Model.barLabel(status, nowMs)
    readonly property string barGlyphText: Model.barGlyphFor(status, nowMs)

    // `Panel` is not `BarWidget`, so the bar geometry it lifts off the host
    // has to be lifted here instead.
    readonly property bool vertical: bar ? bar.vertical : false

    readonly property color foreground: bar ? bar.foreground : Color.foreground
    readonly property color barForegroundColor: bar ? bar.barForeground : Color.foreground
    readonly property color urgent: bar ? bar.urgent : Color.urgent
    readonly property color dim: Qt.darker(foreground, 1.55)
    readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

    // A state maps to a tone, and the tone resolves here against the theme.
    // No colour is ever named in Model.js, so a theme change needs no code.
    function toneColor(tone, base) {
        if (tone === "urgent") return root.urgent;
        if (tone === "dim") return Qt.darker(base, 1.55);
        return base;
    }

    readonly property color barTint:
        toneColor(Model.barToneFor(status, nowMs), root.barForegroundColor)

    // --- the session list, refreshed when the panel opens ---------------

    property var sessions: []
    property string sessionsError: ""
    property int cursorIndex: -1
    property bool cursorActive: false

    function refreshSessions() {
        if (listProcess.running) return;
        root.sessionsError = "";
        listProcess.command = Model.muninArgv(["munin", "list", "--json", "--limit", "5"]);
        listProcess.running = true;
    }

    function run(argv) {
        Quickshell.execDetached(Model.muninArgv(argv));
    }

    function openPath(path) {
        if (!path) return;
        Quickshell.execDetached(Model.muninArgv(["xdg-open", String(path)]));
    }

    function runPrimary() {
        root.run(Model.primaryActionFor(root.status, root.nowMs).argv);
        root.close();
    }

    // The action that cannot live on a notification: the wrapper takes one
    // --exec and the "streams gone" notification spends it on Stop (contracts
    // 8), so the panel and the keybind are the only places *Keep recording*
    // can be offered -- and without it a Teams reconnect during the grace
    // period auto-stops the meeting with no way for the user to say otherwise.
    readonly property var secondary: Model.secondaryActionFor(root.status, root.nowMs)

    function runSecondary() {
        if (!root.secondary) return;
        root.run(root.secondary.argv);
        root.close();
    }

    // --- bar-facing shape ----------------------------------------------

    visible: Model.visible(barState)
    implicitWidth: visible ? button.implicitWidth : 0
    implicitHeight: button.implicitHeight

    onOpenedChanged: {
        // The meters are a live tap on PipeWire. Leaving them running behind
        // a closed popup would cost for nothing.
        if (root.muninService) root.muninService.metersEnabled = root.opened;
        if (!opened) return;
        root.cursorActive = false;
        root.cursorIndex = -1;
        root.nowMs = Date.now();
        root.refreshSessions();
        Qt.callLater(function () { keyCatcher.forceActiveFocus(); });
    }

    // A recording that is running while the panel is shut still has to tick,
    // because the elapsed time is on the bar itself.
    Timer {
        interval: 1000
        repeat: true
        running: root.counting || root.opened
        onTriggered: root.nowMs = Date.now()
    }

    // `done` hides itself 30 s after the transcript landed, and that expiry is
    // computed from `since` rather than written to state.json by a timer
    // (contracts 7.2). Nothing else is counting in `done`, so without one more
    // tick at the right moment the widget would sit there until the daemon
    // next wrote the file. One shot, scheduled for the deadline, rather than a
    // second per-second timer that never stops.
    Timer {
        repeat: false
        running: root.status.state === "done"
        interval: Math.max(250,
            (Model.DONE_VISIBLE_SECONDS + 1
                - Math.max(0, Model.elapsedSeconds(root.status.since, Date.now()))) * 1000)
        onTriggered: root.nowMs = Date.now()
    }

    Process {
        id: listProcess
        running: false
        command: []
        stdout: StdioCollector { id: listStdout; waitForEnd: true }
        stderr: StdioCollector { id: listStderr; waitForEnd: true }
        onExited: function (exitCode) {
            if (exitCode === 0) {
                root.sessions = Model.parseSessions(String(listStdout.text || ""));
                root.sessionsError = "";
                return;
            }
            root.sessions = [];
            // Exit code 3 is the CLI's "daemon unreachable" (contracts 11).
            // Saying so is more use than an empty list that looks like "you
            // have never recorded anything".
            root.sessionsError = exitCode === 3
                ? "The Munin daemon is not running."
                : (String(listStderr.text || "").trim() || "Could not read the session list.");
        }
    }

    // --- the bar button --------------------------------------------------

    WidgetButton {
        id: button
        anchors.fill: parent
        bar: root.bar

        labelVisible: false
        hasVisualContent: root.visible
        // The chip is drawn rather than labelled, so the slot has to be sized
        // from it: WidgetButton only measures its own hidden label.
        fixedWidth: root.vertical ? -1
            : Math.round(chip.implicitWidth + button.scaledHorizontalMargin * 2)
        tooltipText: Model.tooltipText(root.status, root.nowMs)

        onPressed: function (b) {
            if (b === Qt.MiddleButton) root.runPrimary();
            else if (b === Qt.RightButton) root.run(["munin", "toggle"]);
            else root.toggle();
        }

        Row {
            id: chip
            anchors.centerIn: parent
            spacing: Style.space(6)

            // recording and ending: a dot, because a red dot is read at a
            // glance and a glyph is not. Pulsing while recording, steady
            // while the grace period runs -- that difference is the only
            // thing the user has to notice.
            Rectangle {
                id: dot
                visible: Model.barShowsDot(root.barState)
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(8)
                height: width
                radius: width / 2
                color: root.urgent

                SequentialAnimation {
                    running: dot.visible && Model.barPulses(root.barState)
                    loops: Animation.Infinite
                    NumberAnimation {
                        target: dot; property: "opacity"
                        from: 1.0; to: 0.25; duration: 900; easing.type: Easing.InOutSine
                    }
                    NumberAnimation {
                        target: dot; property: "opacity"
                        from: 0.25; to: 1.0; duration: 900; easing.type: Easing.InOutSine
                    }
                    // `ending` is the same red held steady, so the dot has to
                    // land back on full opacity rather than wherever the
                    // animation happened to stop.
                    onRunningChanged: if (!running) dot.opacity = 1.0
                }
            }

            Text {
                id: glyphText
                textFormat: Text.PlainText
                visible: root.barGlyphText !== ""
                anchors.verticalCenter: parent.verticalCenter
                text: root.barGlyphText
                color: root.barTint
                font.family: root.fontFamily
                font.pixelSize: Style.bar.iconFont
                renderType: Text.NativeRendering

                RotationAnimation {
                    target: glyphText
                    property: "rotation"
                    running: glyphText.visible && Model.barSpins(root.barState)
                    loops: Animation.Infinite
                    from: 0
                    to: 360
                    duration: 1600
                    onRunningChanged: if (!running) glyphText.rotation = 0
                }
            }

            Text {
                textFormat: Text.PlainText
                visible: root.barLabel !== "" && !root.vertical
                anchors.verticalCenter: parent.verticalCenter
                text: root.barLabel
                color: root.barTint
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                renderType: Text.NativeRendering
            }
        }
    }

    // --- the dropdown -----------------------------------------------------

    KeyboardPanel {
        id: panel
        anchorItem: button
        owner: root
        bar: root.bar
        open: root.opened
        focusTarget: keyCatcher
        contentWidth: panel.fittedContentWidth(Style.space(360))
        contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

        PanelKeyCatcher {
            id: keyCatcher
            anchors.fill: parent

            onMoveRequested: function (dx, dy) {
                if (!root.cursorActive) { root.cursorActive = true; return; }
                if (dy === 0 || root.sessions.length === 0) return;
                root.cursorIndex = Math.max(0, Math.min(root.sessions.length - 1,
                                                        root.cursorIndex + dy));
            }
            onActivateRequested: {
                if (root.cursorActive && root.cursorIndex >= 0
                    && root.cursorIndex < root.sessions.length)
                    root.openPath(root.sessions[root.cursorIndex].path);
                else root.runPrimary();
            }
            onCloseRequested: root.close()
            onTabRequested: function (direction) { root.switchPanel(direction); }
            onTextKey: function (t) {
                if (t === "r" || t === "R") root.refreshSessions();
                else if (t === "o" || t === "O") root.openPath(root.muninHome);
                // k: keep recording, while the grace period is counting down.
                else if (t === "k" || t === "K") root.runSecondary();
            }

            Flickable {
                id: panelFlick
                anchors.fill: parent
                contentWidth: width
                contentHeight: column.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick
                interactive: contentHeight > height
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                    id: column
                    width: panelFlick.width
                    spacing: Style.space(12)

                    PanelHero {
                        id: hero
                        width: parent.width
                        title: root.heroTitle
                        meta: root.heroMeta
                        foreground: root.foreground
                        fontFamily: root.fontFamily

                        // Munin's identity glyph: U+F0EC2, the one
                        // bar/indicators/ScreenRecording.qml uses for its own
                        // recording indicator.
                        iconComponent: Component {
                            Text {
                                textFormat: Text.PlainText
                                text: Model.GLYPH
                                color: root.toneColor(Model.barTone(root.barState), root.foreground)
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.display
                            }
                        }

                        trailingControl: Component {
                            Text {
                                textFormat: Text.PlainText
                                visible: root.heroElapsed !== ""
                                text: root.heroElapsed
                                color: root.foreground
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.heading
                            }
                        }
                    }

                    // --- tracks ------------------------------------------
                    //
                    // PipeWire's own levels, not Munin's. The plugin has no
                    // access to the capture (D20), so the header says what
                    // these actually are rather than implying they are the
                    // recorded signal.
                    PanelSeparator {
                        visible: trackBlock.visible
                        foreground: root.foreground
                    }

                    Column {
                        id: trackBlock
                        visible: root.counting || root.barState === "detected"
                        width: parent.width
                        spacing: Style.space(8)

                        PanelSectionHeader {
                            text: "LIVE INPUT"
                            foreground: root.foreground
                            fontFamily: root.fontFamily
                        }

                        TrackMeter {
                            width: parent.width
                            label: "You"
                            live: root.muninService !== null
                            level: root.muninService ? root.muninService.micLevel : 0
                            foreground: root.foreground
                            urgent: root.urgent
                            fontFamily: root.fontFamily
                        }

                        TrackMeter {
                            width: parent.width
                            label: "Meeting"
                            live: root.muninService !== null && root.muninService.appNode !== null
                            level: root.muninService ? root.muninService.appLevel : 0
                            foreground: root.foreground
                            urgent: root.urgent
                            fontFamily: root.fontFamily
                        }

                        Text {
                            textFormat: Text.PlainText
                            width: parent.width
                            text: "PipeWire levels, not the recording. Munin captures in its own daemon."
                            color: root.dim
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption
                            wrapMode: Text.WordWrap
                        }
                    }

                    // --- session ------------------------------------------

                    PanelSeparator {
                        visible: sessionBlock.visible
                        foreground: root.foreground
                    }

                    Column {
                        id: sessionBlock
                        visible: root.status.session_id !== null
                            || root.status.detected_app !== null
                        width: parent.width
                        spacing: Style.spacing.labelGap

                        PanelSectionHeader {
                            text: "SESSION"
                            foreground: root.foreground
                            fontFamily: root.fontFamily
                        }

                        InfoPair {
                            visible: root.startedClock !== ""
                            label: "Started"
                            value: root.startedClock
                        }
                        InfoPair {
                            visible: root.sourceLabel !== ""
                            label: "Source"
                            value: root.sourceLabel
                        }
                        InfoPair {
                            visible: root.status.segment > 1
                            label: "Segment"
                            value: String(root.status.segment)
                        }
                        InfoPair {
                            // How long is left before the grace period stops
                            // this recording by itself. Without it "Keep
                            // recording" is a button with no deadline on it.
                            visible: root.graceClock !== ""
                            label: "Stops at"
                            value: root.graceClock
                        }
                        InfoPair {
                            visible: root.status.last_error !== null
                            label: "Error"
                            value: String(root.status.last_error || "")
                        }
                    }

                    // --- actions -------------------------------------------

                    Row {
                        width: parent.width
                        spacing: Style.space(8)

                        Button {
                            text: Model.primaryActionFor(root.status, root.nowMs).label
                            iconText: root.counting ? Model.GLYPH : ""
                            bordered: true
                            foreground: root.counting ? root.urgent : root.foreground
                            fontFamily: root.fontFamily
                            onClicked: root.runPrimary()
                        }

                        Button {
                            visible: root.secondary !== null
                            text: root.secondary ? root.secondary.label : ""
                            bordered: true
                            foreground: root.foreground
                            fontFamily: root.fontFamily
                            onClicked: root.runSecondary()
                        }

                        Button {
                            visible: root.status.session !== null
                            text: "Open session"
                            bordered: true
                            foreground: root.foreground
                            fontFamily: root.fontFamily
                            onClicked: { root.openPath(root.status.session); root.close(); }
                        }
                    }

                    // --- recent --------------------------------------------

                    PanelSeparator { foreground: root.foreground }

                    Column {
                        width: parent.width
                        spacing: Style.space(6)

                        PanelSectionHeader {
                            text: "RECENT"
                            foreground: root.foreground
                            fontFamily: root.fontFamily
                        }

                        Text {
                            textFormat: Text.PlainText
                            visible: root.sessionsError !== ""
                            width: parent.width
                            text: root.sessionsError
                            color: root.urgent
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.bodySmall
                            wrapMode: Text.WordWrap
                        }

                        Text {
                            textFormat: Text.PlainText
                            visible: root.sessionsError === "" && root.sessions.length === 0
                            width: parent.width
                            text: "No sessions yet."
                            color: root.dim
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.body
                            horizontalAlignment: Text.AlignHCenter
                        }

                        Column {
                            id: sessionColumn
                            width: parent.width
                            spacing: Style.space(4)

                            Repeater {
                                model: root.sessions

                                SessionRow {
                                    required property var modelData
                                    required property int index

                                    width: sessionColumn.width
                                    session: modelData
                                    glyph: Model.sessionGlyph(modelData.state)
                                    meta: Model.sessionMeta(modelData)
                                    foreground: root.foreground
                                    urgent: root.urgent
                                    fontFamily: root.fontFamily
                                    hasCursor: root.cursorActive && root.cursorIndex === index
                                    onHovered: {
                                        root.cursorActive = true;
                                        root.cursorIndex = index;
                                    }
                                    onActivated: { root.openPath(modelData.path); root.close(); }
                                }
                            }
                        }
                    }

                    // --- footer --------------------------------------------

                    PanelSeparator { foreground: root.foreground }

                    Row {
                        width: parent.width
                        spacing: Style.space(8)

                        Button {
                            text: "Open recordings folder"
                            iconText: Model.GLYPH_FOLDER
                            foreground: root.foreground
                            fontFamily: root.fontFamily
                            leftAlign: true
                            onClicked: { root.openPath(root.muninHome); root.close(); }
                        }
                    }
                }
            }
        }
    }

    // --- derived text ----------------------------------------------------

    readonly property string muninHome: Quickshell.env("MUNIN_HOME")
        || (Quickshell.env("HOME") + "/munin")

    readonly property string heroTitle: {
        if (root.status.title) return root.status.title;
        if (root.barState === "detected" && root.status.detected_app)
            return root.status.detected_app.label || "Call detected";
        return "Munin";
    }

    readonly property string heroMeta: {
        switch (root.barState) {
        case "detected": return "A call is live. Munin is not recording.";
        case "recording": return "Recording, two tracks.";
        case "ending": return "Streams gone. Stops by itself shortly.";
        case "captured":
        case "transcribing":
            return root.status.queue_depth + " in the queue, audio safe on disk.";
        case "done": return "Transcript ready.";
        case "failed": return "Failed. The audio is kept.";
        default:
            return root.muninService && root.muninService.daemonRunning
                ? "Idle." : "The Munin daemon is not running.";
        }
    }

    readonly property string heroElapsed: {
        if (!root.counting) return "";
        var secs = Model.elapsedSeconds(root.status.started_at, root.nowMs);
        return secs < 0 ? "" : Model.formatElapsedShort(secs);
    }

    readonly property string startedClock: Model.formatClock(root.status.started_at)

    // Only set while the grace period runs (contracts 7.2).
    readonly property string graceClock: root.barState === "ending"
        ? Model.formatClock(root.status.grace_deadline) : ""

    readonly property string sourceLabel: root.status.detected_app
        && root.status.detected_app.label
        ? root.status.detected_app.label + " · " + root.status.detected_app.app_id
        : ""

    // A label and a value on one line, the label dim. Same shape the first
    // party panels use for their key/value blocks.
    component InfoPair: Item {
        id: pair

        property string label: ""
        property string value: ""

        width: parent ? parent.width : 0
        implicitHeight: Math.max(pairLabel.implicitHeight, pairValue.implicitHeight)

        Text {
            id: pairLabel
            textFormat: Text.PlainText
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: pair.label
            color: root.foreground
            opacity: 0.6
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
        }

        Text {
            id: pairValue
            textFormat: Text.PlainText
            anchors.right: parent.right
            anchors.left: pairLabel.right
            anchors.leftMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            horizontalAlignment: Text.AlignRight
            text: pair.value
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
        }
    }

}
