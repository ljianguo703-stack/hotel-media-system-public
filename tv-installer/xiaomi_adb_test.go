package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---- 1.3.4: firmware that switches ADB off at boot (MiTV-ASTP0 field log 2026-10-30) ----

func mitvADBOff(t *testing.T, name string) *tvSim {
	s := newSim(t, name)
	s.brand, s.model, s.device = "Xiaomi", "MiTV-ASTP0", "mulan"
	s.adbOffAtBoot = 27 * time.Second // com.xiaomi.mitv.settings, BOOT_COMPLETED + adb_enabled=0
	return s
}

func TestADBSwitchedOffAfterBootIsCaughtBeforeAnyChangeAndRemembered(t *testing.T) {
	s := mitvADBOff(t, "mitv-adb-off-1")
	e := simEngine(t, s, true)
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "系统会自动关闭 ADB 调试") || !strings.Contains(err.Error(), "没有改动电视") {
		t.Fatal("ADB lost after the pre-check boot was not caught as such", err)
	}
	assertUntouched(t, s)
	if s.installed {
		t.Fatal("installed although the channel was gone")
	}
	id := digest([]byte(s.identity))
	if !e.adbOffKnown(id) {
		t.Fatal("TV not remembered")
	}
	// The operator switches ADB back on in the TV menu and presses install again.
	s.offline, s.adbOpen = false, true
	var logs []string
	req := simRequest(false)
	snap, err := e.install(req, hash(), func(l string) { logs = append(logs, l) })
	if err != nil {
		t.Fatal(err, "\n", strings.Join(logs, "\n"))
	}
	all := strings.Join(logs, "\n")
	if !strings.Contains(all, "跳过预检重启") || !strings.Contains(all, "影院客户端在验证期间保持调试") {
		t.Fatal("second run did not take the keep-ADB route", all)
	}
	if !snap.RebootVerified || !snap.ADBOffAtBoot || s.front != pkg {
		t.Fatal("verification boot not passed with the client keeping ADB", snap.RebootVerified, s.front)
	}
	if s.keepAdb {
		t.Fatal("keep_adb left on after the verification")
	}
	if s.adbOpen {
		t.Fatal("ADB close was requested but not sent")
	}
}

func TestLateVendorSwitchOffIsWaitedFor(t *testing.T) {
	// The vendor handler can run later than the first half minute of a stable channel (busy boot):
	// the channel only counts once the TV has been up long enough for BOOT_COMPLETED handlers.
	s := mitvADBOff(t, "mitv-late-off")
	s.adbOffAtBoot = 55 * time.Second
	e := simEngine(t, s, true)
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "系统会自动关闭 ADB 调试") {
		t.Fatal("late boot-time switch-off passed the pre-check", err)
	}
	assertUntouched(t, s)
}

func TestSwitchOffSeenInTheSettingBeforeTheLinkDrops(t *testing.T) {
	s := mitvADBOff(t, "mitv-lingering-link")
	s.lingerAfterOff = 5 * time.Minute
	e := simEngine(t, s, true)
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "系统会自动关闭 ADB 调试") {
		t.Fatal("adb_enabled=0 behind a link that still answers was not noticed", err)
	}
	assertUntouched(t, s)
}

func TestADBLeftOpenOnRequestStaysKeptByTheClient(t *testing.T) {
	s := mitvADBOff(t, "mitv-keep-open")
	s.xiaomiLog = true
	e := simEngine(t, s, true)
	req := simRequest(false)
	req.CloseADB = false
	snap, err := e.install(req, hash(), func(string) {})
	if err != nil || !snap.RebootVerified || !s.keepAdb || !s.adbOpen {
		t.Fatal("ADB left open on request must stay kept across boots", err, s.keepAdb, s.adbOpen)
	}
	s.reboot()
	s.clock += time.Minute
	s.advance()
	if s.offline || !s.adbOpen {
		t.Fatal("next boot closed ADB although the operator kept it open")
	}
}

func TestADBOffSeenInThisBootsLogNeedsNoExtraRound(t *testing.T) {
	// The boss's TV after 15:50: the current boot's log already shows the vendor switching ADB off.
	s := mitvADBOff(t, "mitv-adb-off-log")
	s.xiaomiLog = true
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil || !snap.RebootVerified {
		t.Fatal("one-click install on a TV whose log shows the boot-time switch-off", err)
	}
	for _, c := range s.calls[:5] {
		if c == "reboot" {
			t.Fatal("pre-check reboot not skipped")
		}
	}
}

func TestKeepADBClientFailingAfterBootIsReportedNotSucceeded(t *testing.T) {
	// The client does not come up at boot (guard never binds after the reboot): nothing puts ADB back.
	s := mitvADBOff(t, "mitv-client-dead")
	s.xiaomiLog = true
	e := simEngine(t, s, true)
	e.Wait = func(d time.Duration) {
		s.clock += d
		if s.bootID >= 1 && s.installed {
			s.guardNeverBinds = true
		}
		s.advance()
	}
	var logs []string
	snap, err := e.install(simRequest(false), hash(), func(l string) { logs = append(logs, l) })
	if err != nil {
		t.Fatal(err)
	}
	if snap.RebootVerified || !snap.RebootUnreachable || snap.ADBCloseRequested || !strings.Contains(strings.Join(logs, "\n"), "影院客户端没能把它重新打开") {
		t.Fatal("lost channel after the verification boot not reported", snap.RebootVerified, logs)
	}
}

func TestRestoreOnADBOffTVIsDoneButBootScreenUnchecked(t *testing.T) {
	s := mitvADBOff(t, "mitv-restore")
	s.xiaomiLog = true
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.adbOpen, s.offline = true, false
	err = e.restore(snap, RestoreOptions{RebootCheck: true})
	if !errors.Is(err, errRestoreNotRechecked) || s.installed {
		t.Fatal("fresh restore on an ADB-off TV", err, s.installed)
	}
}

func TestLostInstallReplyIsCheckedAndRolledBack(t *testing.T) {
	s := newSim(t, "lost-reply")
	s.installReplyLost = true
	e := simEngine(t, s, true)
	var logs []string
	_, err := e.install(simRequest(false), hash(), func(l string) { logs = append(logs, l) })
	if err == nil || !strings.Contains(err.Error(), "回执丢失") || !strings.Contains(err.Error(), "已执行恢复") || s.installed {
		t.Fatal("installed package behind a lost reply not rolled back", err, s.installed)
	}
	if !strings.Contains(strings.Join(logs, "\n"), "电视返回：") {
		t.Fatal("raw adb reply not logged")
	}
	// Channel gone as well: the state is unknown and must be said so.
	u := newSim(t, "lost-reply-offline")
	u.installReplyLost = true
	eu := simEngine(t, u, true)
	eu.Runner = offlineAfterInstall{u}
	_, err = eu.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "无法确认") {
		t.Fatal(err)
	}
}

type offlineAfterInstall struct{ *tvSim }

func (r offlineAfterInstall) Run(serial string, args ...string) (string, error) {
	out, err := r.tvSim.Run(serial, args...)
	if len(args) > 0 && args[0] == "install" {
		r.tvSim.offline = true
	}
	return out, err
}

func TestVendorBootLogParsing(t *testing.T) {
	// Lines from the MiTV-ASTP0 logcat of 2026-10-30.
	boot := "10-30 15:44:52.262 D/Settings( 4115): [SettingsBroadcastReceiver]---action = android.intent.action.BOOT_COMPLETED\n10-30 15:44:52.262 E/MiTv-Utils( 4115): isTvProductFromCharacters character = tv\n10-30 15:44:52.262 D/SettingsUtil( 4115): setSettingsValue  android.provider.Settings$Global.putInt(adb_enabled,0)\n"
	user := "10-30 15:50:03.577 D/settings( 7406): ADB debug is off\n10-30 15:50:06.173 D/SettingsUtil( 7406): setSettingsValue  android.provider.Settings$Global.putInt(adb_enabled,1)\n"
	userOff := "10-30 16:10:00.000 D/SettingsUtil( 7406): setSettingsValue  android.provider.Settings$Global.putInt(adb_enabled,0)\n"
	otherPid := "10-30 15:44:52.262 D/Settings( 4115): action = android.intent.action.BOOT_COMPLETED\n10-30 15:44:52.900 D/SettingsUtil( 9999): putInt(adb_enabled,0)\n"
	late := "10-30 15:44:52.262 D/Settings( 4115): action = android.intent.action.BOOT_COMPLETED\n10-30 15:44:58.000 D/SettingsUtil( 4115): putInt(adb_enabled,0)\n"
	if !adbOffInLog(boot) || !adbOffInLog(user+boot) || adbOffInLog(user) || adbOffInLog(userOff) || adbOffInLog(otherPid) || adbOffInLog(late) {
		t.Fatal("vendor boot-time ADB switch-off detection")
	}
}

func TestADBOffRecordHoldsNoAddressOrRawIdentity(t *testing.T) {
	s := mitvADBOff(t, "mitv-record")
	e := simEngine(t, s, true)
	e.install(simRequest(false), hash(), func(string) {})
	b, err := os.ReadFile(filepath.Join(e.State, adbOffFile))
	if err != nil || strings.Contains(string(b), s.identity) || strings.Contains(string(b), "192.168") {
		t.Fatal(string(b), err)
	}
}

// guardedPilotKeepingADB: an earlier install left the client keeping ADB on (operator unticked
// "close debugging"), so this TV's boot-time switch-off is repaired by the client at once.
func guardedPilotKeepingADB(t *testing.T, name string) (*tvSim, []byte) {
	s := mitvADBOff(t, name)
	pilot := []byte("pilot-keeps-adb")
	s.installed, s.installedAPK, s.wss, s.keepAdb = true, pilot, true, true
	s.secure["enabled_accessibility_services"] = guard
	s.secure["accessibility_enabled"], s.secure["sim_pin"] = "1", "1"
	s.front = pkg
	return s, pilot
}

func TestClientAlreadyKeepingADBIsNotUndone(t *testing.T) {
	// Review round 6, finding 1: the pre-check boot passes because the installed client puts ADB
	// back; the tool must still see the firmware's switch-off and must not clear keep_adb.
	s, pilot := guardedPilotKeepingADB(t, "mitv-pilot-keeps-adb")
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	req := simRequest(false)
	req.CloseADB = false
	var logs []string
	snap, err := e.install(req, hash(), func(l string) { logs = append(logs, l) })
	if err != nil || !snap.RebootVerified || !snap.ADBOffAtBoot || !s.keepAdb {
		t.Fatal("client keeping ADB undone or switch-off missed", err, snap.RebootVerified, snap.ADBOffAtBoot, s.keepAdb, "\n", strings.Join(logs, "\n"))
	}
	if !e.adbOffKnown(digest([]byte(s.identity))) {
		t.Fatal("TV not remembered")
	}
}

func TestKeepADBUntouchedOnOrdinaryUpgradeAndRestore(t *testing.T) {
	// No switch-off on this TV: the client's own keep_adb preference is neither cleared by the
	// install (debugging left open) nor by the restore.
	s, pilot := guardedPilotKeepingADB(t, "keeps-adb-no-vendor")
	s.adbOffAtBoot = 0
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	req := simRequest(false)
	req.CloseADB = false
	snap, err := e.install(req, hash(), func(string) {})
	if err != nil || !s.keepAdb || snap.KeepADBSet {
		t.Fatal("keep_adb changed by an install that did not need it", err, s.keepAdb)
	}
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil || !s.keepAdb {
		t.Fatal("keep_adb cleared by the restore", err, s.keepAdb)
	}
	// With "close debugging" ticked the preference is cleared, or the client would reopen it.
	u, upilot := guardedPilotKeepingADB(t, "keeps-adb-close")
	u.adbOffAtBoot = 0
	eu := simEngine(t, u, true)
	allowAPK(eu, upilot)
	if _, err = eu.install(simRequest(false), hash(), func(string) {}); err != nil || u.keepAdb || u.adbOpen {
		t.Fatal("close requested but the client still keeps ADB", err, u.keepAdb)
	}
}

func TestRestoreOfKeepADBRunPutsThePreferenceBack(t *testing.T) {
	// This run turned keep_adb on for an older client that did not have it: a restore turns it off.
	s, pilot := pilotWithoutTakeover(t, "mitv-pilot-plain", 28)
	s.adbOffAtBoot, s.xiaomiLog = 27*time.Second, true
	s.brand, s.model, s.device = "Xiaomi", "MiTV-ASTP0", "mulan"
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	req := simRequest(false)
	req.CloseADB = false
	snap, err := e.install(req, hash(), func(string) {})
	if err != nil || !snap.KeepADBSet || !s.keepAdb {
		t.Fatal(err, snap.KeepADBSet, s.keepAdb)
	}
	e.restore(snap, RestoreOptions{})
	if s.keepAdb {
		t.Fatal("keep_adb this run set was left on after the restore")
	}
}

// pullFails: the link drops again while the installed APK is being read back.
type pullFails struct {
	*tvSim
	installed *bool
}

func (r pullFails) Run(serial string, args ...string) (string, error) {
	if len(args) > 0 && args[0] == "install" {
		*r.installed = true
	}
	if len(args) > 0 && args[0] == "pull" && *r.installed {
		return "adb: error: failed to stat remote object", errors.New("exit status 1")
	}
	return r.tvSim.Run(serial, args...)
}

func TestLostReplyOnUpgradeIsNeverTakenForNothingInstalled(t *testing.T) {
	// Review round 6, finding 8: on an upgrade a failed read-back must not count as "the old client
	// is still there" — the new one may be installed, so the run is rolled back.
	s, pilot := pilotWithoutTakeover(t, "lost-reply-upgrade", 28)
	s.installReplyLost = true
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	e.Runner = pullFails{s, new(bool)}
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "无法确认") || !strings.Contains(err.Error(), "恢复") {
		t.Fatal("unverifiable upgrade treated as nothing installed", err)
	}
}
