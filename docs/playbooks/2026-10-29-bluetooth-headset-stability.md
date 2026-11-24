# Bluetooth headset stability on this machine (2026-10-29)

Machine-local findings and changes. Nothing here is Munin code; it is the
audio stack Munin records from.

## Symptom

Headset microphone and playback drop mid-meeting; the far end hears
crackling; a power-cycle of the headset brings it back. Headset: Jabra
EVOLVE 65 over the on-board Intel Bluetooth controller (USB 8087:0036,
same package as the Wi-Fi 7 radio).

## What the journal showed

- WirePlumber: `Failure in Bluetooth audio transport .../fd58` (the HFP call
  link) about three times a day since the start of the journal, four times
  inside one five-minute Teams call on 2026-10-29. Each one takes both
  `bluez_input` and `bluez_output` to `error` at once: mic and sound drop
  together.
- Kernel: `SCO packet for unknown connection handle` seconds after each one.
- The negotiated HFP codec was mSBC (snapshot `~/.cache/munin/audiofix/`).
- The controller had USB autosuspend on: `power/control=auto`, 2 s delay.
  `usbcore autosuspend=-1` does not cover it; `btusb` enables it itself.
- Wi-Fi was associated (5 GHz) while Ethernet was also up.
- The headset's USB cable enumerates as HID only, not as a sound card, so
  "use it wired" is not an option. Its intended PC path is the Jabra Link
  370 dongle (USB audio class, no host Bluetooth involved).

## Changes

1. `~/.config/wireplumber/wireplumber.conf.d/bluetooth-hfp-cvsd.conf`:
   `bluez5.enable-msbc = false`. The call link now uses CVSD (8 kHz voice)
   instead of mSBC. Delete the file to get wideband back once the link is
   proven stable.
2. `nmcli radio wifi off` while on Ethernet, to take Wi-Fi/Bluetooth
   coexistence out of the picture.
3. Root-only, run by the user: `options btusb enable_autosuspend=0` in
   `/etc/modprobe.d/btusb.conf`, a udev rule pinning `power/control=on` for
   8087:0036, and the same written to `/sys` for the running controller.

## How to tell whether it worked

```bash
journalctl --user -u wireplumber --since today -o short-iso | grep 'audio transport' | grep -v sep
journalctl -k --since today | grep 'SCO packet'
cat /sys/bus/usb/devices/3-10/power/control   # must say "on"
```

Zero lines from the first two across a day of meetings means the link held.

## If it still drops

The remaining variables are radio: the controller sits inside a mini PC
next to a USB 3 hub (a known 2.4 GHz noise source). A Jabra Link 370
dongle, or the USB speakerphone already attached, bypasses the on-board
controller entirely.

## Follow-up 2026-11-02: drops moved to the join, settle delay added

First meeting day after the changes above. Four Teams recordings, 2 h 17 min,
zero mid-call drops. Every failure came 6 to 34 s after the HFP link came up,
and only when the headset had been powered on 2 s before WirePlumber switched
it to the headset profile (Teams already held the microphone). Joins with
11 s or more between power-on and the switch never failed. A power-cycle
always recovered it because the second connect was followed by a switch
the headset could take.

A `btmon` capture of a clean join confirmed CVSD is in use (`+BCS: 1`) and
that the WirePlumber line `Failure in Bluetooth audio transport .../fdNN`
fires on every *intentional* SCO teardown (HCI reason 0x16, local host),
including Teams releasing and re-acquiring the device at join and every
hangup. On its own it means nothing. A real drop is that line plus
bluetoothd `ext_io_disconnected ... Hands-Free Voice gateway` (the HFP link
itself went) with no `capture stopped` in `munin.log` just before.

### Change 4: defer the headset-profile switch after connect

`~/.local/share/wireplumber/scripts/device/autoswitch-bluetooth-profile.lua`
is a copy of the stock 0.5.18 script with a `device-added` hook: a Bluetooth
device added less than `HEADSET_SETTLE_MSEC` (10 000 ms) ago is not switched
to its headset profile; the switch is deferred and the profiles are
re-evaluated when the timer expires. It must live under `$XDG_DATA_HOME`
(`~/.local/share`), not `~/.config`; WirePlumber 0.5 does not look for
scripts there. Delete the file to get stock behaviour back; after a
WirePlumber upgrade, diff it against `/usr/share/wireplumber/scripts/device/`
and re-apply the four marked blocks.

`~/.config/wireplumber/wireplumber.conf.d/log-level-notice.conf` raises the
log level to notice so the deferral is visible.

### How to tell whether it worked (replaces the greps above)

After powering the headset on and joining within 10 s:

```bash
journalctl --user -u wireplumber --since today | grep -E 'Deferring profile switch|has settled'
journalctl --since today -o short-iso | grep -E 'Hands-Free Voice gateway|capture stopped'
```

The first should show a deferral followed by a re-evaluation about 10 s
after connect. In the second, a "Hands-Free Voice gateway" line that is not
preceded by a "capture stopped" line or a deliberate power-off is a drop.
The cost of the change: when the headset is powered on during a call, the
microphone arrives up to 10 s later than before.

## Follow-up 2026-11-24: dead microphone on the second call, HFP reset added

Joins held after change 4. What remained: a call that arrives while the
headset is still connected from an earlier call (a direct call, typically)
starts with a dead microphone.

### What the journal showed

43 switches to the headset profile, 2026-11-04 to 2026-11-24:

- 7 logged `pw.node: (bluez_input.<MAC>.0-N) running -> error` within about
  30 ms of the switch. The microphone is dead from the first second. Each
  ended 16 to 23 s later, with `lost RFCOMM connection` or a restore to
  A2DP at 16 s.
- All 7 were a second or later switch on the same Bluetooth connection, 5 to
  182 s after the previous restore. 0 of 36 first switches after a connect
  failed.
- The erroring node is the internal call node, `bluez_input.<MAC with
  underscores>.0`, media class `Audio/Source/Internal`. The source that
  applications see (`bluez_input.<MAC with colons>`, a loopback) does not
  error.

### Cause

Trace logging (`wpctl set-log-level 'D,spa.bluez5*:T'`, back with
`wpctl set-log-level 3`) shows `sco_ready ... acquire failed: Too many links
(-31)` 4 ms after the SCO connect. The previous call's SCO link is still held
by the controller or headset although the kernel has dropped it (`SCO packet
for unknown connection handle` long after teardown). The new link is refused.

- The state does not clear by itself (90 s tested). Switching the card
  through `off` does not clear it. A full `bluetoothctl disconnect` and
  `connect` does.
- Reproduction without Teams: keep a `pw-play` tone on the headset, then
  `timeout 5 pw-record a.wav`, wait, `timeout 9 pw-record b.wav`. The second
  recording is under 1 s of silence. The gap does not matter (0.7 to 20 s
  all fail).
- Trigger: a playback stream that ran during the first call is still playing
  at the second switch. Silence at either point, or a stream started after
  the first call, does not trigger it.
- Not Munin: it reproduces with `munin.service` and `munin-work.service`
  stopped, and a private sink with a loopback during the first call neither
  causes nor prevents it. Not tested: the shell plugin's level meters.
- What below PipeWire leaves the link behind is not known. `sudo btmon`
  across a hangup would show whether the SCO disconnect completes.

### Change 5: reset the hands-free link after each call

Resetting only the Handsfree profile clears the stale link in 1.2 s while
A2DP keeps playing, and the device is not re-added, so the 10 s settle delay
does not apply:

```bash
bluetoothctl disconnect <MAC> 0000111e-0000-1000-8000-00805f9b34fb
bluetoothctl connect    <MAC> 0000111e-0000-1000-8000-00805f9b34fb
```

Three machine-local pieces automate it:

- The autoswitch override from change 4 logs two markers:
  `headset-hfp-left card=bluez_card.<MAC>` on every restore from the headset
  profile, and `headset-sco-dead node=bluez_input.<MAC>.0` when the call
  node errors within 1.5 s of a switch. The node hook needs the interest
  `media.class matches "Audio/Source*"` because of the `Internal` class.
  These are further local blocks to re-apply after a WirePlumber upgrade;
  diff the override against the stock script to find them all.
- `~/.local/bin/headset-sco-recover` follows the WirePlumber journal for the
  markers. WirePlumber's Lua sandbox cannot run a command, hence a separate
  process. On `headset-hfp-left` it waits 1 s and resets the hands-free link,
  unless the microphone is already in use again (prevention). On
  `headset-sco-dead` it resets at once and puts the card back in the headset
  profile if a call still holds the microphone (repair). It never runs sudo.
- `~/.config/systemd/user/headset-sco-recover.service` runs it, part of
  `graphical-session.target`, after `wireplumber.service`.

To undo: `systemctl --user disable --now headset-sco-recover.service`. The
markers alone do nothing.

### Results

| Test | Before | After |
|---|---|---|
| 5 synthetic calls 7 s apart, tone playing throughout | second call dead every time | 5 of 5 live |
| Synthetic dead start, prevention off | dead until a reconnect | live about 3 s into the call |
| 3 Teams calls on 2026-11-24 (12:09, 13:05, 14:01) | | no drop noticed by the user |

The 13:05 call started 1 s after the previous one ended, so prevention was
skipped and the call started dead. Repair fired and the headset profile was
back 1.4 s after the switch. The other two started clean.

### How to tell whether it worked

```bash
journalctl --user -u headset-sco-recover --since today
```

One `reset HFP after call` line per hangup is prevention. A `call mode
started dead` line means repair ran; expect it when one call follows another
within about 1 s. A dead start with neither line means the marker was missed.

### Still open

- Five early losses (2026-11-12 10:40, 11-16 09:05, 11-23 10:02, 11-24 09:01
  and 10:20) showed no node error. Cause unknown; a `sudo btmon -w` capture
  across one would give the HCI reason.
- Whether the far end hears the 1.4 s gap at a repaired start has not been
  checked against a recording.
- The mic preflight signal S1 `node-error` (spec `2026-10-14-mic-preflight.md`)
  is still unimplemented and not verified. If it is built, it must watch the
  internal call node, not the loopback source.
