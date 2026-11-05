package main

import "testing"

func TestVerifiedIdlePolicyIsBackedUpAppliedAndRestored(t *testing.T) {
	s := newSim(t, "idle-policy")
	s.brand, s.model, s.device = "Xiaomi", "MiTV-ASTP0", "mulan"
	s.fingerprint = "Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys"
	s.system = map[string]string{"screen_saver_timeout": "120000", "screen_off_timeout": "2147483647"}
	s.secure["screensaver_enabled"] = "1"
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if snap.NativeIdle["system/screen_saver_timeout"] != "120000" || snap.NativeIdle["secure/screensaver_enabled"] != "1" {
		t.Fatal("original settings were not recorded", snap.NativeIdle)
	}
	if s.system["screen_saver_timeout"] != "-1" || s.secure["screensaver_enabled"] != "0" {
		t.Fatal("idle policy missing")
	}
	if s.system["screen_off_timeout"] != "2147483647" {
		t.Fatal("power timeout changed")
	}
	s.adbOpen = true
	if err = e.restore(snap); err != nil {
		t.Fatal(err)
	}
	if s.system["screen_saver_timeout"] != "120000" || s.secure["screensaver_enabled"] != "1" {
		t.Fatal("restore did not put original settings back")
	}
}
func TestUnknownFirmwareAndLegacySnapshotDoNotWriteIdleSettings(t *testing.T) {
	s := newSim(t, "idle-unknown")
	e := simEngine(t, s, true)
	f := Facts{Model: "MiTV-ASTP0", Device: "mulan", SDK: 28, Fingerprint: "different-firmware"}
	if verifiedNativeIdle(f) {
		t.Fatal("unverified firmware matched")
	}
	if err := e.applyNativeIdle("192.168.4.62:5555", Snapshot{Facts: f}, func(string) {}); err != nil {
		t.Fatal(err)
	}
	var failures []string
	e.restoreNativeIdle("192.168.4.62:5555", Snapshot{}, &failures)
	if len(s.calls) != 0 || len(failures) != 0 {
		t.Fatal("unknown settings were changed")
	}
}
