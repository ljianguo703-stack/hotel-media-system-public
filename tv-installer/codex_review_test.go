package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---- Regression tests for the Codex review of 1.3.0 (one group per finding) ----

// pilotWithoutTakeover is a TV that already had an older hotel client installed, but never as its
// HOME: no guard, no WRITE_SECURE_SETTINGS, no overlay op.
func pilotWithoutTakeover(t *testing.T, name string, sdk int) (*tvSim, []byte) {
	s := newSim(t, name)
	s.sdk = sdk
	pilot := []byte("pilot-no-takeover-apk")
	s.installed, s.installedAPK = true, pilot
	s.secure["sim_pin"] = "1"
	return s, pilot
}

func TestUpgradeRestoreTakesBackGrantsSoTheOldClientCannotRetakeHome(t *testing.T) {
	// Finding 1. Android 9 (background starts allowed), 11 (overlay op needed) and 13 (restricted
	// settings op as well).
	for _, sdk := range []int{28, 30, 33} {
		s, pilot := pilotWithoutTakeover(t, "pilot-no-takeover-"+fmt.Sprint(sdk), sdk)
		e := simEngine(t, s, true)
		allowAPK(e, pilot)
		snap, err := e.install(simRequest(false), hash(), func(string) {})
		if err != nil {
			t.Fatal(sdk, err)
		}
		if snap.Grants == nil || !snap.Grants.WSSKnown || !snap.Grants.OpsKnown || snap.Grants.WSS || snap.Grants.AppOps["SYSTEM_ALERT_WINDOW"] != "default" {
			t.Fatalf("%d: original grants not recorded: %+v", sdk, snap.Grants)
		}
		if !s.wss || s.ops["SYSTEM_ALERT_WINDOW"] != "allow" {
			t.Fatal(sdk, "install did not grant what the takeover needs; the test would prove nothing")
		}
		s.adbOpen = true
		var logs []string
		if err = e.restore(snap, RestoreOptions{RebootCheck: true, Log: func(l string) { logs = append(logs, l) }}); err != nil {
			t.Fatal(sdk, err, "\n", strings.Join(logs, "\n"))
		}
		if string(s.installedAPK) != string(pilot) || s.wss || len(s.ops) != 0 || hasGuard(s.secure["enabled_accessibility_services"]) {
			t.Fatalf("%d: not back to the pilot's own state: apk=%q wss=%v ops=%v a11y=%q", sdk, s.installedAPK, s.wss, s.ops, s.secure["enabled_accessibility_services"])
		}
		// Two more cold boots: the guard must stay off and HOME must stay with the vendor launcher.
		for i := 0; i < 2; i++ {
			s.reboot()
			if hasGuard(s.secure["enabled_accessibility_services"]) {
				t.Fatal(sdk, "old client switched its guard back on after a reboot")
			}
			if sdk >= 29 && s.front == pkg {
				t.Fatal(sdk, "old client brought itself to the front at boot with an overlay op it did not have before")
			}
			s.pressHome()
			if s.front != "com.vendor.tvhome" {
				t.Fatal(sdk, "HOME taken back after restore:", s.front)
			}
		}
		if s.secure["sim_pin"] != "1" {
			t.Fatal(sdk, "the pilot's data was wiped")
		}
	}
}

func TestOldUpgradeRestoreIsUndoneByBootReceiver(t *testing.T) {
	// Negative control: the 1.3.0 upgrade restore (older APK back, guard removed, HOME handed back,
	// but WRITE_SECURE_SETTINGS and the overlay op left in place). The simulator must show the
	// takeover coming back, otherwise the test above would prove nothing.
	s, pilot := pilotWithoutTakeover(t, "negative-control-upgrade", 30)
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	s.installedAPK = pilot
	s.secure["enabled_accessibility_services"] = mergeService(s.secure["enabled_accessibility_services"], guard, false)
	s.preferred = s.vendor[0]
	s.front = componentPackage(s.vendor[0])
	s.reboot()
	if !hasGuard(s.secure["enabled_accessibility_services"]) || s.front != pkg {
		t.Fatal("simulator does not model the boot-time revival that Codex described")
	}
}

func TestUpgradeRestoreOfRecordWithoutGrantsInfersFromTakeover(t *testing.T) {
	// Records written by 1.2.0 and 1.3.0 have no grant state. A client that was not taking over must
	// not keep the permissions; one that was must keep them.
	s, pilot := pilotWithoutTakeover(t, "legacy-record-no-takeover", 30)
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	snap.Grants = nil
	e.saveSnapshot(snap)
	s.adbOpen = true
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal(err)
	}
	if s.wss || len(s.ops) != 0 {
		t.Fatal("inferred restore left permissions or app ops behind", s.wss, s.ops)
	}

	g := newSim(t, "legacy-record-guarded-pilot")
	g.sdk = 30
	gp := []byte("pilot-guard-apk")
	g.installed, g.installedAPK, g.wss = true, gp, true
	g.ops["SYSTEM_ALERT_WINDOW"] = "allow"
	g.secure["enabled_accessibility_services"] = "com.voice/com.voice.Reader:" + guard
	g.front = pkg
	ge := simEngine(t, g, true)
	allowAPK(ge, gp)
	gsnap, err := ge.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	gsnap.Grants = nil
	g.adbOpen = true
	if err = ge.restore(gsnap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal(err)
	}
	if !g.wss || g.front != pkg {
		t.Fatal("a pilot that was taking over lost its own permission", g.wss, g.front)
	}
}

func TestGuardedPilotKeepsItsOwnGrants(t *testing.T) {
	s := newSim(t, "guarded-pilot-grants")
	s.sdk = 30
	pilot := []byte("pilot-guard-apk")
	s.installed, s.installedAPK, s.wss = true, pilot, true
	s.ops["SYSTEM_ALERT_WINDOW"] = "allow"
	s.secure["enabled_accessibility_services"] = "com.voice/com.voice.Reader:" + guard
	s.front = pkg
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if !snap.Grants.WSSKnown || !snap.Grants.OpsKnown || !snap.Grants.WSS || snap.Grants.AppOps["SYSTEM_ALERT_WINDOW"] != "allow" {
		t.Fatalf("%+v", snap.Grants)
	}
	s.adbOpen = true
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal(err)
	}
	if !s.wss || s.ops["SYSTEM_ALERT_WINDOW"] != "allow" || s.ops["GET_USAGE_STATS"] != "" || s.front != pkg {
		t.Fatal("pilot not restored to exactly its own grants", s.wss, s.ops, s.front)
	}
}

func TestRestoreReportsAGrantThatCannotBeTakenBack(t *testing.T) {
	s, pilot := pilotWithoutTakeover(t, "revoke-ignored", 30)
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	r := revokeIgnored{s}
	e.Runner = r
	if err = e.restore(snap); err == nil || !strings.Contains(err.Error(), "修改系统设置权限") {
		t.Fatal("a kept WRITE_SECURE_SETTINGS was not reported", err)
	}
}

type revokeIgnored struct{ *tvSim }

func (r revokeIgnored) Run(serial string, args ...string) (string, error) {
	if len(args) == 2 && args[0] == "shell" && strings.Contains(args[1], "'revoke'") {
		return "\n" + rcMarker + "0", nil // exits 0 and changes nothing (seen on some vendor builds)
	}
	return r.tvSim.Run(serial, args...)
}

func TestPermissionDumpParsing(t *testing.T) {
	for dump, want := range map[string][2]bool{
		// Android 9: granted.
		"  Package [" + pkg + "] (1a):\n    requested permissions:\n      " + wssPermission + "\n    install permissions:\n      " + wssPermission + ": granted=true\n": {true, true},
		// Requested only: never counts.
		"  Package [" + pkg + "] (1a):\n    requested permissions:\n      " + wssPermission + "\n    install permissions:\n      android.permission.INTERNET: granted=true\n": {false, true},
		// Android 12 after a revoke.
		"  Package [" + pkg + "] (1a):\n    install permissions:\n      " + wssPermission + ": granted=false, flags=[ USER_SET ]\n": {false, true},
		// Runtime section (some builds list development permissions there).
		"  Package [" + pkg + "] (1a):\n    User 0: ceDataInode=1 installed=true\n      runtime permissions:\n        " + wssPermission + ": granted=true, flags=[ ]\n": {true, true},
		// Android 5 list.
		"  Package [" + pkg + "] (1a):\r\n    grantedPermissions:\r\n      " + wssPermission + "\r\n": {true, true},
		// Another package in the same dump holds it.
		"  Package [com.other] (1b):\n    install permissions:\n      " + wssPermission + ": granted=true\n  Package [" + pkg + "] (1a):\n    install permissions:\n      android.permission.INTERNET: granted=true\n": {false, true},
		"Unable to find package: " + pkg: {false, false},
	} {
		g, k := wssGranted(dump)
		if g != want[0] || k != want[1] {
			t.Fatalf("got %v,%v want %v for\n%s", g, k, want, dump)
		}
	}
	ops := parseAppOps("Uid mode: SYSTEM_ALERT_WINDOW: deny\nSYSTEM_ALERT_WINDOW: allow; time=+1m ago\nGET_USAGE_STATS: ignore\nMIUIOP(10008): allow\n")
	if ops["SYSTEM_ALERT_WINDOW"] != "allow" || ops["GET_USAGE_STATS"] != "ignore" || len(ops) != 2 {
		t.Fatal(ops)
	}
	tv := planGrants(Snapshot{Installed: true, FromTV: true, VendorHome: pkg, Grants: &ClientGrants{WSSKnown: true, WSS: true, OpsKnown: true, AppOps: map[string]string{"SYSTEM_ALERT_WINDOW": "allow"}}, Facts: Facts{SDK: 30}})
	if tv.WSS || tv.Ops["SYSTEM_ALERT_WINDOW"] != "default" {
		t.Fatal("a restore point read from the TV kept or granted a takeover permission", tv)
	}
}

func TestEveryUnverifiedTVGetsThePreflightReboot(t *testing.T) {
	// Finding 2: the first TV of a firmware came back after its reboot; the second TV of the same
	// firmware has debugging that does not survive one. It must still be checked, and refused.
	first := newSim(t, "same-firmware-1")
	e := simEngine(t, first, false)
	if _, err := e.install(simRequest(true), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if ok, _ := e.fieldHistory(Facts{Fingerprint: first.fingerprint}); ok != 1 {
		t.Fatal("first install not recorded", ok)
	}
	second := newSim(t, "same-firmware-2")
	second.rebootUnreachable = true
	e.Runner = second
	e.Wait = func(d time.Duration) { second.clock += d; second.advance() }
	_, err := e.install(simRequest(true), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "没有改动电视") {
		t.Fatal("second TV of a known firmware skipped the reboot pre-check", err)
	}
	assertUntouched(t, second)
}

// ipTakeover answers for `first` until the switch, then for `second` at the same address (a DHCP
// lease handed to another device while the first TV reboots).
type ipTakeover struct {
	first, second *tvSim
	swapped       bool
	swapAtReboot  int // switch when the address receives its Nth reboot (0 = never)
	reboots       int
	afterIDReads  int // switch right after this many android_id reads (0 = never)
	idReads       int
}

func (r *ipTakeover) Run(serial string, args ...string) (string, error) {
	if r.swapped {
		return r.second.Run(serial, args...)
	}
	out, err := r.first.Run(serial, args...)
	if len(args) > 0 && args[0] == "reboot" {
		r.reboots++
		if r.swapAtReboot > 0 && r.reboots >= r.swapAtReboot {
			r.swapped, r.first.offline = true, true
		}
	}
	if len(args) > 1 && strings.Contains(args[1], "android_id") {
		r.idReads++
		if r.afterIDReads > 0 && r.idReads >= r.afterIDReads {
			r.swapped = true
		}
	}
	return out, err
}

func assertUntouched(t *testing.T, s *tvSim) {
	t.Helper()
	for _, c := range s.calls {
		if strings.HasPrefix(c, "install") || strings.HasPrefix(c, "push") || strings.Contains(c, "'put'") || strings.Contains(c, "'grant'") || strings.Contains(c, "set-home-activity") {
			t.Fatalf("%s changed: %s", s.name, c)
		}
	}
}

func TestPreflightRebootChecksItIsTheSameTV(t *testing.T) {
	// Finding 3a: after the pre-check reboot another TV of the same model answers at the address.
	first, second := newSim(t, "ip-owner"), newSim(t, "ip-taker")
	second.bootID = 5 // boot ids are random per device
	r := &ipTakeover{first: first, second: second, swapAtReboot: 1}
	e := simEngine(t, first, false)
	e.Runner = r
	e.Wait = func(d time.Duration) { first.clock += d; second.clock += d; second.advance() }
	_, err := e.install(simRequest(true), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "不是原来那台电视") {
		t.Fatal("install went on with another TV at the address", err)
	}
	assertUntouched(t, first)
	assertUntouched(t, second)
	if entries, _ := os.ReadDir(filepath.Join(e.State, "backups")); len(entries) != 0 {
		t.Fatal("a backup was written for the wrong TV")
	}
}

func TestNoRebootIsSentToADeviceThatIsNotTheTV(t *testing.T) {
	// Finding 3b: the address changes hands during the wait before the reboot.
	first, second := newSim(t, "ip-owner-2"), newSim(t, "ip-taker-2")
	r := &ipTakeover{first: first, second: second}
	e := simEngine(t, first, false)
	e.Runner = r
	e.Wait = func(d time.Duration) {
		if d == 15*time.Second {
			r.swapped = true
		}
	}
	_, err := e.install(simRequest(true), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "不是原来那台电视") {
		t.Fatal(err)
	}
	for _, s := range []*tvSim{first, second} {
		for _, c := range s.calls {
			if c == "reboot" {
				t.Fatal(s.name, "was rebooted")
			}
		}
	}
	assertUntouched(t, second)
}

func TestAddressChangingHandsAtAnyPointBeforeTheFirstChangeStops(t *testing.T) {
	// Finding 3 and review point 2: the address answers with another TV after the Nth identity
	// read — before the backup, in the middle of it, before the restore point, before the APK.
	// Identity reads: 1 detection, 2–3 backup start/end, 4 before the pre-check reboot, then the
	// reads while the boot settles, one before the restore point and one before the APK. The last
	// two are located in a clean run. Switching right after read 4 would hand the reboot command
	// itself to the other device (a window of one command, not modelled further).
	beforePush, beforeInstall := idReadsBefore(t, "'mkdir' '-p' '/sdcard/hotel-tv-restore'"), idReadsBefore(t, "install")
	for n, want := range map[int]string{1: "已不是检测的那台电视", 2: "设备变了", 3: "预检重启前后核对身份时", beforePush - 1: "改动电视前核对身份未通过", beforeInstall - 1: "安装前核对身份未通过"} {
		first, second := newSim(t, "owner-"+fmt.Sprint(n)), newSim(t, "taker-"+fmt.Sprint(n))
		second.bootID = 7 // boot ids are random per device
		r := &ipTakeover{first: first, second: second, afterIDReads: n}
		e := simEngine(t, first, true)
		e.Runner = r
		_, err := e.install(simRequest(false), hash(), func(string) {})
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Fatal(n, err)
		}
		for _, s := range []*tvSim{first, second} {
			for _, c := range s.calls {
				// The right TV may get its pre-check reboot; the other device never any.
				if strings.HasPrefix(c, "install") || strings.Contains(c, "'put'") || strings.Contains(c, "'grant'") || strings.Contains(c, "set-home-activity") || (c == "reboot" && s == second) {
					t.Fatalf("%d: %s changed: %s", n, s.name, c)
				}
			}
		}
		if n < beforeInstall-1 {
			assertUntouched(t, second)
		}
		if entries, _ := os.ReadDir(filepath.Join(e.State, "backups")); n <= 3 && len(entries) != 0 {
			t.Fatal(n, "a backup of the wrong TV was kept")
		}
	}
}

// flakyID fails the Nth android_id read once, the way a dropped shell or busy settings provider does.
type flakyID struct {
	*tvSim
	failAt, reads int
}

func (r *flakyID) Run(serial string, args ...string) (string, error) {
	if len(args) > 1 && args[0] == "shell" && strings.Contains(args[1], "android_id") {
		r.reads++
		if r.reads == r.failAt {
			return "error: closed", fmt.Errorf("exit 1")
		}
	}
	return r.tvSim.Run(serial, args...)
}

func TestOneFailedIdentityReadNeverSkipsTheColdBootCheck(t *testing.T) {
	// Review point 1: a single glitch on any identity read must neither skip the cold-boot check nor
	// be reported as an address conflict. The TV here fails its cold boot, so the only correct end
	// is "cold boot failed, restored".
	for n := 1; n <= 12; n++ {
		s := newSim(t, "flaky-id-"+fmt.Sprint(n))
		s.rebootLosesHome = true
		e := simEngine(t, s, false)
		e.Runner = &flakyID{tvSim: s, failAt: n}
		_, err := e.install(simRequest(true), hash(), func(string) {})
		if err == nil || !strings.Contains(err.Error(), "软件重启后") || !strings.Contains(err.Error(), "已执行恢复") || strings.Contains(err.Error(), "占用") {
			t.Fatal(n, err)
		}
	}
}

func TestPolicyRefusalComesBeforeThePreflightReboot(t *testing.T) {
	// Review point 5: a TV that will be turned down anyway is not rebooted first.
	s := newSim(t, "unlisted-client")
	s.installed, s.installedAPK = true, []byte("some-newer-client")
	e := simEngine(t, s, false)
	_, err := e.install(simRequest(true), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "可覆盖清单") {
		t.Fatal(err)
	}
	for _, c := range s.calls {
		if c == "reboot" {
			t.Fatal("rebooted a TV that was then refused")
		}
	}
}

func TestPilotWithPermissionButGuardMomentarilyOffIsATakeover(t *testing.T) {
	// Review point 4: backed up while the firmware had just cleared its guard. With the permission it
	// switches the guard back on by itself, so its own behaviour is the takeover, and the restore
	// must not fail on it (nor take its permission away).
	s := newSim(t, "pilot-guard-cleared")
	s.sdk = 30
	pilot := []byte("pilot-guard-apk")
	s.installed, s.installedAPK, s.wss = true, pilot, true
	s.ops["SYSTEM_ALERT_WINDOW"] = "allow"
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if hasGuard(snap.Accessibility) || !snap.Grants.WSS {
		t.Fatal("test setup: record should show the permission without the guard")
	}
	s.adbOpen = true
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal("restore failed on the pilot's own behaviour:", err)
	}
	if !s.wss || s.front != pkg || !hasGuard(s.secure["enabled_accessibility_services"]) {
		t.Fatal("pilot not back to taking over", s.wss, s.front)
	}
	// An older record (no grants) with only the revival listener on is not evidence of a takeover:
	// a guess never adds one (re-review C).
	if p := planGrants(Snapshot{Installed: true, Notification: listener}); p.WSS || clientTakesOver(Snapshot{Installed: true, Notification: listener}) {
		t.Fatal("a guess turned a non-takeover pilot into a takeover")
	}
}

type opsLie struct {
	*tvSim
	hideOps bool
}

func (r opsLie) Run(serial string, args ...string) (string, error) {
	if len(args) == 2 && args[0] == "shell" {
		if strings.Contains(args[1], "'appops' 'set'") && strings.Contains(args[1], "SYSTEM_ALERT_WINDOW' 'default'") {
			return "\n" + rcMarker + "0", nil // accepted, changes nothing
		}
		if r.hideOps && strings.Contains(args[1], "'appops' 'get'") {
			return "Permission Denial\n" + rcMarker + "1", nil
		}
	}
	return r.tvSim.Run(serial, args...)
}

func TestTVsOwnPowerOnAppIsNotTakenForATakeover(t *testing.T) {
	// Re-review A: the TV itself is set to open the hotel client at power-on. With the overlay op read
	// back as it was before, the client in front at boot is the TV's own setting, not a failure.
	s, pilot := pilotWithoutTakeover(t, "power-on-app", 30)
	s.bootApp = pkg
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal("healthy TV failed the restore check:", err)
	}
	if s.wss || len(s.ops) != 0 || hasGuard(s.secure["enabled_accessibility_services"]) {
		t.Fatal("not back to the pilot's own state")
	}
}

func TestLateBootMessageOpeningTheClientIsCaught(t *testing.T) {
	// Re-review B: overlay op unreadable and its reset ignored, and BOOT_COMPLETED reaches the client
	// only after the vendor launcher settled in front.
	s, pilot := pilotWithoutTakeover(t, "late-boot-message", 30)
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	snap.Grants = nil
	s.adbOpen = true
	s.receiverDelay = 50 * time.Second
	e.Runner = opsLie{tvSim: s, hideOps: true}
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err == nil || !strings.Contains(err.Error(), "自己打开到了前台") {
		t.Fatal("client opening itself late at boot passed", err)
	}
}

func TestOverlayOpThatStaysAllowedIsCaught(t *testing.T) {
	// Review point 3: the overlay op is read back; where it cannot be, the cold boot decides.
	s, pilot := pilotWithoutTakeover(t, "overlay-stays", 30)
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	e.Runner = opsLie{tvSim: s}
	if err = e.restore(snap); err == nil || !strings.Contains(err.Error(), "SYSTEM_ALERT_WINDOW") {
		t.Fatal("overlay op left at allow was not reported", err)
	}
	s2, pilot2 := pilotWithoutTakeover(t, "overlay-stays-unreadable", 30)
	e2 := simEngine(t, s2, true)
	allowAPK(e2, pilot2)
	snap2, err := e2.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	snap2.Grants = nil // older record: ops inferred, and the TV now hides them
	s2.adbOpen = true
	e2.Runner = opsLie{tvSim: s2, hideOps: true}
	if err = e2.restore(snap2, RestoreOptions{RebootCheck: true}); err == nil || !strings.Contains(err.Error(), "自己打开到了前台") {
		t.Fatal("client opening itself at boot with a leftover overlay op passed", err)
	}
}

func TestUnreadablePermissionsFallBackToTheBootCheck(t *testing.T) {
	// A TV whose permission dump cannot be read and whose revoke silently does nothing: only the
	// post-restore cold boot can tell, and it must not pass.
	s, pilot := pilotWithoutTakeover(t, "no-package-dump", 30)
	s.noPkgDump = true
	// BOOT_COMPLETED reaches the client late: after the boot has settled (about 100 s of uptime)
	// and after the first half minute of the watch that follows.
	s.receiverDelay = 115 * time.Second
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if snap.Grants == nil || snap.Grants.WSSKnown {
		t.Fatal("unreadable permission recorded as known", snap.Grants)
	}
	s.adbOpen = true
	e.Runner = revokeIgnored{s}
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err == nil || !strings.Contains(err.Error(), "重新打开了守护") {
		t.Fatal("a guard switched back on after the restore reboot passed", err)
	}
}

func TestPostInstallRebootOntoAnotherTVIsNeverRestoredThrough(t *testing.T) {
	// The cold-boot verification lands on another device: never claim success, never run the
	// automatic restore against that device.
	first, second := newSim(t, "verified-owner"), newSim(t, "verified-taker")
	second.bootID = 5
	r := &ipTakeover{first: first, second: second, swapAtReboot: 2} // after the pre-check reboot, at the verification boot
	e := simEngine(t, first, true)
	e.Runner = r
	e.Wait = func(d time.Duration) { first.clock += d; second.clock += d }
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "不是这台电视") || !strings.Contains(err.Error(), "拒绝跨电视恢复") {
		t.Fatal("verification on another TV not reported as a failure, or restore went through it", err)
	}
	if snap.RebootVerified || snap.ADBCloseRequested {
		t.Fatal("verification on another TV accepted", snap.RebootVerified)
	}
	assertUntouched(t, second)
}

func TestVerifiedFirmwareWithTemporaryDebuggingIsRefusedBeforeAnyChange(t *testing.T) {
	// Codex review of 1.3.1: another TV of this project's own verified firmware, with debugging
	// switched on only until the next reboot. It must be refused before anything changes.
	s := newSim(t, "verified-temporary-adb")
	s.rebootUnreachable = true
	e := simEngine(t, s, true)
	if d := decide(e.Catalog, Facts{Brand: s.brand, Model: s.model, Device: s.device, SDK: s.sdk, Fingerprint: s.fingerprint}); d.Tier != "verified" {
		t.Fatal("test setup: expected the verified tier", d.Tier)
	}
	req := simRequest(false)
	req.SkipRebootCheck = true // the operator switching off the verification boot does not skip the pre-check
	_, err := e.install(req, hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "没有改动电视") {
		t.Fatal("verified firmware skipped the reboot pre-check", err)
	}
	assertUntouched(t, s)
	if entries, _ := os.ReadDir(filepath.Join(e.State, "backups")); len(entries) != 0 {
		t.Fatal("a backup of a refused attempt was kept")
	}
}

// idReadsBefore counts the android_id reads of a clean install up to the first command that
// contains marker (a prefix match for top-level adb commands such as "install").
func idReadsBefore(t *testing.T, marker string) int {
	s := newSim(t, "count-id-reads")
	e := simEngine(t, s, true)
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, c := range s.calls {
		if strings.HasPrefix(c, marker) || (strings.HasPrefix(c, "shell") && strings.Contains(c, marker)) {
			return n
		}
		if strings.Contains(c, "android_id") {
			n++
		}
	}
	t.Fatal("marker not seen:", marker)
	return 0
}
