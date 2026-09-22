// Fixture for test_proc_walk.mjs. Not part of the plugin.
//
// Walks /proc from this process upwards twice, once the way Service.qml does
// it now (each hop started from a zero-interval Timer) and once the way it did
// before (the next path assigned inside the FileView's own onLoaded). Prints a
// hop count for each. The first is what detection depends on; the second is
// the platform quirk that broke it, kept here so an upstream fix shows up as a
// number rather than as a surprise.

import QtQuick
import Quickshell
import Quickshell.Io

ShellRoot {
    Item {
        id: root

        property int startPid: 0
        property int deferredHops: 0
        property int directHops: 0

        function ppidOf(text) {
            var lines = String(text || "").split("\n");
            for (var i = 0; i < lines.length; i++)
                if (lines[i].indexOf("PPid:") === 0)
                    return parseInt(lines[i].substring(5).replace(/\s+/g, ""), 10);
            return 0;
        }

        // --- the pattern the plugin uses -------------------------------

        property int deferredPid: 0

        FileView {
            id: deferred
            watchChanges: false
            printErrors: false
            onLoaded: {
                root.deferredHops += 1;
                var ppid = root.ppidOf(deferred.text());
                if (root.deferredHops < 3 && ppid > 0) {
                    root.deferredPid = ppid;
                    deferredRead.restart();
                }
            }
            onLoadFailed: root.deferredHops += 100   // loud, and not a hop
        }

        Timer {
            id: deferredRead
            interval: 0
            repeat: false
            onTriggered: {
                deferred.path = "/proc/" + root.deferredPid + "/status";
                deferred.reload();
            }
        }

        // --- the pattern that swallowed every hop after the first -------

        FileView {
            id: direct
            watchChanges: false
            printErrors: false
            onLoaded: {
                root.directHops += 1;
                var ppid = root.ppidOf(direct.text());
                if (root.directHops < 3 && ppid > 0) {
                    direct.path = "/proc/" + ppid + "/status";
                    direct.reload();
                }
            }
            onLoadFailed: root.directHops += 100
        }

        Component.onCompleted: {
            root.startPid = Number(Quickshell.env("MUNIN_TEST_PID")) || 1;
            root.deferredPid = root.startPid;
            deferredRead.restart();
            direct.path = "/proc/" + root.startPid + "/status";
            direct.reload();
        }

        Timer {
            interval: 4000
            repeat: false
            running: true
            onTriggered: {
                console.log("WALK deferred=" + root.deferredHops + " direct=" + root.directHops);
                Qt.quit();
            }
        }
    }
}
