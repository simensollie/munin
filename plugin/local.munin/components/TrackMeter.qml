// One row of the panel's Tracks block: a label, a level bar and a reading.
//
// What it shows is what PipeWire is doing right now, not what munin-rec wrote
// to disk. The plugin has no access to the capture at all (D20), so this is a
// live-input indication -- "the microphone is picking you up", "the call is
// making sound" -- and the panel labels it that way rather than implying it
// is the recorded signal.
//
// Purely presentational: a caller sets `level` between 0 and 1 and the row
// draws it. No PipeWire import here, so the same component renders a fake
// level in a sketch as happily as a real one in the bar.
//
// Owner: plugin workstream.

import QtQuick
import qs.Commons

Item {
    id: root

    property string label: ""
    property real level: 0            // 0 .. 1, linear peak
    property bool live: true          // false dims the row: nothing is bound to it
    property color foreground: Color.foreground
    property color urgent: Color.urgent
    property string fontFamily: Style.font.family

    readonly property color dim: Qt.darker(foreground, 1.55)
    readonly property real clamped: Math.max(0, Math.min(1, Number(level) || 0))

    // Peak in dBFS, which is how an audio level is read everywhere else in
    // this stack. Silence is a dash rather than "-inf dB", because a column
    // of minus-infinities is noise on its own.
    readonly property string reading: !live ? "--"
        : (clamped <= 0.0005 ? "--"
            : Math.round(20 * Math.log(clamped) / Math.LN10) + " dB")

    width: parent ? parent.width : implicitWidth
    implicitHeight: Math.max(labelText.implicitHeight, Style.space(14))
    opacity: live ? 1.0 : 0.45

    Text {
        id: labelText
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(58)
        text: root.label
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight
    }

    Rectangle {
        id: track
        anchors.left: labelText.right
        anchors.leftMargin: Style.space(8)
        anchors.right: readingText.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        height: Style.space(6)
        radius: height / 2
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)

        Rectangle {
            width: Math.round(track.width * root.clamped)
            height: parent.height
            radius: parent.radius
            // Over about -6 dBFS a meeting track is close enough to clipping
            // that it is worth saying so before the recording, not after.
            color: root.clamped > 0.5 ? root.urgent : root.foreground
            opacity: 0.85

            Behavior on width {
                NumberAnimation { duration: 90; easing.type: Easing.OutQuad }
            }
        }
    }

    Text {
        id: readingText
        textFormat: Text.PlainText
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(46)
        horizontalAlignment: Text.AlignRight
        text: root.reading
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
    }
}
