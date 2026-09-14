// One row of the panel's Recent block: a state glyph, a title and a meta line.
//
// Clicking a row opens its directory in the file manager. There is no "play"
// and no "delete": the PoC deliberately has one destructive path (the file
// manager) rather than a button in a bar popup that can remove a meeting
// recording with a mis-click.
//
// Owner: plugin workstream.

import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Ui

CursorSurface {
    id: root

    property var session: null
    property string glyph: ""
    property string meta: ""
    property color urgent: Color.urgent
    property string fontFamily: Style.font.family

    readonly property string title: session && session.title ? String(session.title) : "Untitled"
    readonly property bool failed: !!session && String(session.state) === "failed"
    readonly property color dim: Qt.darker(foreground, 1.55)

    signal activated()
    // The panel owns the cursor, not the row: CursorSurface's contract is that
    // exactly one highlight exists on screen whether it came from the mouse or
    // the keyboard, which only holds if hover reports upwards instead of
    // painting itself.
    signal hovered()

    implicitHeight: rowContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onEntered: root.hovered()
        onClicked: root.activated()
    }

    RowLayout {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: Style.space(10)
        anchors.rightMargin: Style.space(10)
        spacing: Style.space(8)

        Text {
            textFormat: Text.PlainText
            text: root.glyph
            color: root.failed ? root.urgent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.icon
            Layout.alignment: Qt.AlignVCenter
        }

        ColumnLayout {
            id: rowContent
            Layout.fillWidth: true
            spacing: Style.space(1)

            Text {
                textFormat: Text.PlainText
                Layout.fillWidth: true
                text: root.title
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
            }

            Text {
                textFormat: Text.PlainText
                visible: root.meta !== ""
                Layout.fillWidth: true
                text: root.meta
                color: root.failed ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
            }
        }
    }
}
