# `pw-dump` fixtures for the microphone preflight (D28)

Built from one real `pw-dump` taken on 2026-10-14 at 14:28 by the prototype
repair script's snapshot step, with a Bluetooth headset connected on A2DP and
the defaults consistent. Reduced to the objects
`munin.capture.health` and `munin doctor` read: the `default` Metadata object,
the `Audio/Sink` and `Audio/Source` nodes, and the bluez card device.

**Sanitised.** Every Bluetooth address is `AA:BB:CC:DD:EE:FF` (and
`AA_BB_CC_DD_EE_FF` where PipeWire uses underscores), the headset is named
`Headset`, the USB microphone `USB Microphone`. Every property the checks do not
read was dropped. What is real is the structure, and one fact that shaped the
check: the card's `bluez5.profile` property reads `"off"` in this *healthy*
dump. The active profile is `info.params.Profile`, which is what S2 reads.

| File | Derived how | Expected |
|---|---|---|
| `healthy-bluetooth.json` | the snapshot | `ok`, label `Headset` |
| `ghost-s3.json` | the default sink node (`bluez_output.AA_BB_CC_DD_EE_FF.1`) removed | `degraded`, `default-sink-missing` alone |
| `ghost-s2.json` | the card's active profile set to `off`, still `connected`, both Bluetooth nodes removed | `degraded`, `profile-off` (S3 also holds; S2 is checked first) |
| `ghost-s1.json` | **pending**: no dump was taken while a ghost existed | — |
