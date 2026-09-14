// Munin bar widget: the recording state, always visible while recording (D10).
//
// States rendered, one at a time (spec 9.2): idle hides, detected is a dim mic
// glyph, recording is a pulsing red dot with elapsed time, ending is steady,
// transcribing shows the queue depth, done shows a tick for 30 s, failed stays
// until acknowledged -- because a failure that hides itself is a lost meeting.
//
// Actions run the CLI. The widget never opens the daemon socket.
//
// Owner: plugin workstream.

import QtQuick
import Quickshell
import qs.Ui

BarWidget {
    id: root

    moduleName: "local.munin"

    readonly property var muninService: bar?.shell?.firstPartyServiceFor("local.munin")
    readonly property string state: muninService?.status?.state ?? "idle"

    visible: root.state !== "idle"

    onClicked: root.bar.run("munin toggle")
}
