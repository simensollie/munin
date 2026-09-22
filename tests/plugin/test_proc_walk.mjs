// The one thing in the plugin that cannot be tested through Model.js: the
// /proc parent walk that turns an audio pid into the window that owns it
// (spec 6.3, condition 2). Every Chromium-based client, Teams included, plays
// its call audio from a child process that owns no window, so a walk that
// stops early identifies nothing -- and the walk stopping early is exactly
// what broke detection on 2026-09-22.
//
// This needs a running Quickshell and a Wayland session, which a checkout on a
// build machine does not have, so it skips rather than fails there.

import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = join(here, "proc_walk.qml");

function quickshellAvailable() {
  if (!process.env.WAYLAND_DISPLAY) return false;
  const probe = spawnSync("sh", ["-c", "command -v qs"], { encoding: "utf8" });
  return probe.status === 0;
}

// `qs -p <file>` runs one shell instance and exits on Qt.quit(). The fixture
// prints a single WALK line; anything else on stderr is Quickshell's own noise.
function runFixture() {
  const out = execFileSync("qs", ["-p", fixture], {
    encoding: "utf8",
    timeout: 30000,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, MUNIN_TEST_PID: String(process.pid) },
  });
  const line = out.split("\n").find((l) => l.includes("WALK "));
  assert.ok(line, "fixture printed no WALK line:\n" + out);
  const m = /WALK deferred=(\d+) direct=(\d+)/.exec(line);
  assert.ok(m, "unparsable WALK line: " + line);
  return { deferred: Number(m[1]), direct: Number(m[2]) };
}

test("the /proc walk completes when each hop is deferred out of onLoaded",
  { skip: quickshellAvailable() ? false : "needs a Wayland session and `qs`" },
  () => {
    const { deferred, direct } = runFixture();

    // Three hops from this process: the walk Service.qml performs. A count of
    // 1 is the bug -- the first hop lands and nothing follows it.
    assert.equal(deferred, 3,
      `deferred walk stopped after ${deferred} hop(s); detection needs the whole walk`);

    // Not an assertion about our code: a canary on the platform quirk the
    // deferral works around. If this ever reaches 3, Quickshell has fixed
    // reassigning FileView.path from inside its own onLoaded, and the
    // Timer in Service.qml is belt without braces rather than load-bearing.
    if (direct === 3) {
      console.log("note: FileView now chains reads directly; the deferral is no longer load-bearing");
    } else {
      assert.equal(direct, 1,
        `expected the direct chain to stop after 1 hop, got ${direct}`);
    }
  });
