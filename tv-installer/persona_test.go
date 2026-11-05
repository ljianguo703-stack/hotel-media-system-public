package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// tvSim is a stateful TV for replay tests. Unlike fakeADB it models the behaviours that broke or
// could break field installs, each tied to a documented source:
//   - transport carries no exit status (pre-shell_v2 adbd): only the in-band marker tells
//   - `pm path` of a missing package: empty output, exit 1 (MiTV-ASTP0 Android 9, the 1.0.0 failure)
//   - Android 10+ prints topResumedActivity; `dumpsys window windows` may lack mCurrentFocus
//   - several launchers and no default: the system answers with ResolverActivity
//   - a HOME query without CATEGORY_DEFAULT drops the stored default (Aerial #767, XGIMI)
//   - the client's BootReceiver re-adds its guard on boot while WRITE_SECURE_SETTINGS is granted
//   - vendor launchers taking HOME back seconds later, cold boot losing HOME, ADB gone after reboot
//   - TCL package verification refusing the HOME-declaring build; streamed install failing
type tvSim struct {
	t                *testing.T
	name             string
	brand, model     string
	device           string
	sdk              int
	crypto           string
	fingerprint      string
	android12Dumps   bool
	crlf             bool
	noDumpActivities bool
	// A vendor system overlay owns window focus (assumed model of always-on voice/notification
	// bars); only the resumed activity tells which app is really in front.
	overlayFocus bool

	identity     string
	vendor       []string // vendor HOME candidates, first is the system default
	preferred    string   // explicit default HOME ("" = none)
	installed    bool
	installedAPK []byte
	wss          bool
	ops          map[string]string // app ops of the client that are not "default"
	system       map[string]string
	secure       map[string]string
	global       map[string]string
	front        string
	bootID       int
	files        map[string][]byte
	adbOpen      bool

	dropPreferredWithoutDefault bool
	damaged                     bool
	vendorRetakeAfter           int
	retakeCountdown             int
	rebootLosesHome             bool
	rebootUnreachable           bool
	unreachableFromBoot         int // debugging lost from this boot on (0 = never)
	offline                     bool
	verificationRejectsHome     bool
	streamingBroken             bool
	guardNeverBinds             bool
	uninstallFails              bool
	silentGrantFailure          bool // synthetic: non-zero exit with no text at all
	setHomeRefused              bool // vendor refuses set-home-activity / role changes
	noSelfRepair                bool // client does not re-add its guard when started
	vendorProp                  string
	port                        string // current debugging port; "" = any
	portAfterReboot             string // wireless debugging hands out a new port on boot
	noBootID                    bool
	ignoreReboot                bool
	uptime                      float64
	// Virtual clock (advanced by Engine.Wait) for firmware that acts on a timer after boot.
	clock, bootAt        time.Duration
	clearEvery           time.Duration // firmware clears the accessibility list this often (T966D5 ≈ 52 s)
	repairDelay          time.Duration // client re-adds its guard this long after a clear (0 = never)
	nextClear, repairAt  time.Duration
	homeBrokenAfterClear bool // after the first clear HOME stays on the vendor launcher
	clearFromBoot        int  // timed clearing starts with this boot (0 = every boot)
	everCleared          bool
	noBrief              bool   // Android 7.x: cmd package resolve/query reject --brief
	bootApp              string // TV set to boot into live TV / an input, not the launcher
	noPkgDump            bool   // `dumpsys package` refused (seen on locked-down vendor builds)
	// BOOT_COMPLETED reaches the client this long after boot (0 = at once). On busy TVs it can
	// arrive well after sys.boot_completed=1.
	receiverDelay, receiverAt time.Duration
	// MiTV-ASTP0: com.xiaomi.mitv.settings writes adb_enabled=0 this long after every boot (0 = never).
	adbOffAtBoot     time.Duration
	adbOffDone       bool
	xiaomiLog        bool // this boot's log holds the vendor BOOT_COMPLETED → adb_enabled=0 lines
	keepAdb          bool // the client's keep_adb preference
	dropOnce         bool // the connection drops once (adbd restarted by the client)
	installReplyLost bool // the package is installed but adb never gets the reply
	// lingerAfterOff: after the vendor switch-off the open connection still answers this long
	// (adbd not restarted at once); only adb_enabled shows the change.
	lingerAfterOff, offlineAt time.Duration
	// The client's saved connection (HotelProvisioning) and its per-run backups.
	provServer, provToken string
	provBackup            map[string][3]string

	calls []string
}

func newSim(t *testing.T, name string) *tvSim {
	s := &tvSim{t: t, name: name, brand: "Test", model: "TestTV", device: "test-chip", sdk: 28, fingerprint: "test/fingerprint", uptime: 300,
		identity: "sim-identity-" + name, vendor: []string{"com.vendor.tvhome/.MainActivityUserMode"},
		secure: map[string]string{"enabled_accessibility_services": "com.voice/com.voice.Reader", "accessibility_enabled": "1", "android_id": "sim-identity-" + name},
		global: map[string]string{"adb_enabled": "1"}, files: map[string][]byte{}, adbOpen: true, ops: map[string]string{}}
	s.front = componentPackage(s.vendor[0])
	return s
}

func (s *tvSim) home() string {
	if s.preferred != "" {
		return s.preferred
	}
	cands := s.candidates()
	if len(cands) == 1 {
		return cands[0]
	}
	return "android/com.android.internal.app.ResolverActivity"
}
func (s *tvSim) candidates() []string {
	out := append([]string{}, s.vendor...)
	if s.installed && string(s.installedAPK) == "payload-standard" {
		out = append(out, activity)
	}
	return out
}
func (s *tvSim) guardActive() bool {
	return s.installed && !s.guardNeverBinds && s.secure["accessibility_enabled"] == "1" && hasGuard(s.secure["enabled_accessibility_services"])
}
func (s *tvSim) pressHome() {
	if s.homeBrokenAfterClear && s.everCleared {
		s.front = componentPackage(s.vendor[0])
		return
	}
	h := s.home()
	if componentPackage(h) == pkg || s.guardActive() {
		s.front = pkg
		if s.vendorRetakeAfter > 0 {
			s.retakeCountdown = s.vendorRetakeAfter
		}
		return
	}
	if strings.Contains(h, "ResolverActivity") {
		s.front = "android"
		return
	}
	s.front = componentPackage(h)
}

// advance applies timed firmware behaviour up to the current virtual time.
func (s *tvSim) advance() {
	if s.clearEvery > 0 && s.installed && s.bootID > 0 && s.bootID >= s.clearFromBoot {
		for s.clock >= s.nextClear {
			s.secure["enabled_accessibility_services"] = mergeService(s.secure["enabled_accessibility_services"], guard, false)
			s.secure["accessibility_enabled"] = "0"
			s.everCleared = true
			if s.front == pkg {
				s.front = componentPackage(s.vendor[0])
			}
			if s.repairDelay > 0 && s.wss {
				s.repairAt = s.nextClear + s.repairDelay
			}
			s.nextClear += s.clearEvery
		}
	}
	if s.repairAt > 0 && s.clock >= s.repairAt {
		s.secure["enabled_accessibility_services"] = mergeService(s.secure["enabled_accessibility_services"], guard, true)
		s.secure["accessibility_enabled"] = "1"
		s.repairAt = 0
		// The guard, bound again, finds the vendor launcher in front and takes over (unless this
		// firmware keeps HOME broken after a clear).
		if !s.homeBrokenAfterClear && s.front == componentPackage(s.vendor[0]) {
			s.front = pkg
		}
	}
	if s.adbOffAtBoot > 0 && s.bootID > 0 && !s.adbOffDone && s.clock >= s.bootAt+s.adbOffAtBoot {
		s.adbOffDone, s.xiaomiLog = true, true
		if s.installed && s.wss && s.keepAdb && s.guardActive() {
			s.dropOnce = true // GuardSetup.ensureAdb puts it straight back; adbd restarts once
		} else {
			s.global["adb_enabled"], s.offline, s.adbOpen = "0", true, false
			if s.lingerAfterOff > 0 {
				s.offline, s.offlineAt = false, s.clock+s.lingerAfterOff
			}
		}
	}
	if s.offlineAt > 0 && s.clock >= s.offlineAt {
		s.offline, s.offlineAt = true, 0
	}
	if s.receiverAt > 0 && s.clock >= s.receiverAt {
		s.receiverAt = 0
		s.ensureEnabled()
		if s.guardActive() {
			s.front = pkg // HotelHomeGuardService.onBootCompleted → take over
		} else {
			s.bringBack()
		}
	}
}

// ensureEnabled is GuardSetup.ensureEnabled(): nothing without WRITE_SECURE_SETTINGS.
func (s *tvSim) ensureEnabled() {
	if s.installed && s.wss {
		s.secure["enabled_accessibility_services"] = mergeService(s.secure["enabled_accessibility_services"], guard, true)
		s.secure["accessibility_enabled"] = "1"
	}
}

// bringBack is BootReceiver with the guard not running: startActivity(MainActivity.bringBackIntent).
// Background starts are allowed up to Android 9; from 10 only with the overlay op.
func (s *tvSim) bringBack() {
	if s.installed && !s.guardActive() && (s.sdk < 29 || s.ops["SYSTEM_ALERT_WINDOW"] == "allow") {
		s.front = pkg
	}
}

func (s *tvSim) reboot() {
	if s.ignoreReboot {
		return
	}
	s.bootAt, s.nextClear, s.repairAt = s.clock, s.clock+s.clearEvery, 0
	s.adbOffDone, s.xiaomiLog, s.offlineAt = false, false, 0
	s.bootID++
	s.uptime = 40
	if s.portAfterReboot != "" {
		s.port, s.portAfterReboot = s.portAfterReboot, ""
	}
	if s.rebootUnreachable || (s.unreachableFromBoot > 0 && s.bootID >= s.unreachableFromBoot) {
		s.offline = true
		return
	}
	// BootReceiver → GuardSetup.ensureEnabled(): re-adds the guard whenever the permission is held.
	if s.receiverDelay == 0 {
		s.ensureEnabled()
	}
	s.retakeCountdown = 0
	s.pressHome()
	if s.rebootLosesHome && s.front == pkg {
		s.front = componentPackage(s.vendor[0])
	}
	if s.bootApp != "" && s.front != pkg {
		s.front = s.bootApp
	}
	if s.receiverDelay == 0 {
		s.bringBack()
	} else {
		s.receiverAt = s.clock + s.receiverDelay
	}
}
func (s *tvSim) dumpPackage() string {
	if !s.installed {
		return ""
	}
	out := "Packages:\n  Package [" + pkg + "] (4f1a2b):\n    userId=10087\n    requested permissions:\n      " + wssPermission + "\n      android.permission.SYSTEM_ALERT_WINDOW\n"
	if s.sdk < 23 {
		out += "    grantedPermissions:\n      android.permission.INTERNET\n"
		if s.wss {
			out += "      " + wssPermission + "\n"
		}
		return out
	}
	out += "    install permissions:\n      android.permission.INTERNET: granted=true\n"
	out += "      " + wssPermission + ": granted=" + fmt.Sprint(s.wss) + "\n"
	return out + "    User 0: ceDataInode=1234 installed=true hidden=false\n      runtime permissions:\n"
}
func (s *tvSim) dumpActivities() string {
	if s.noDumpActivities {
		return "Service dumps disabled"
	}
	comp := s.front + "/.Main"
	if s.front == pkg {
		comp = activity
	}
	if s.android12Dumps {
		return "ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n  topResumedActivity=ActivityRecord{9f1 u0 " + comp + " t31}\n  ResumedActivity: ActivityRecord{9f1 u0 " + comp + " t31}"
	}
	if s.sdk < 24 {
		// Android 5–6 layout: the home stack's task carries the HOME intent.
		home := s.home()
		return "ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n  Stack #0:\n    Task id #1\n    * TaskRecord{41f3a1c #1 A=" + componentPackage(home) + " U=0 sz=1}\n      userId=0 effectiveUid=1000 mCallingUid=0 mCallingPackage=null\n      intent={act=android.intent.action.MAIN cat=[android.intent.category.HOME] flg=0x10000000 cmp=" + home + "}\n  Stack #1:\n    mResumedActivity: ActivityRecord{c2e4 u0 " + comp + " t2}"
	}
	return "ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n    mResumedActivity: ActivityRecord{c2e4 u0 " + comp + " t2}"
}
func (s *tvSim) dumpWindow(windowsOnly bool) string {
	comp := s.front + "/.Main"
	if s.front == pkg {
		comp = activity
	}
	if windowsOnly && s.android12Dumps {
		return "WINDOW MANAGER WINDOWS (dumpsys window windows)\n  Window #0 Window{1 u0 NavigationBar0}:"
	}
	if s.overlayFocus {
		return "  mCurrentFocus=Window{3c2 u0 com.vendor.voice/com.vendor.voice.FloatBar}"
	}
	return "  mCurrentFocus=Window{7d1 u0 " + comp + "}\n  mFocusedApp=ActivityRecord{c2e4 u0 " + comp + " t2}"
}

func (s *tvSim) reachable(serial string) bool {
	return serial == "" || s.port == "" || strings.HasSuffix(serial, ":"+s.port)
}

func (s *tvSim) Run(serial string, args ...string) (string, error) {
	s.calls = append(s.calls, strings.Join(args, " "))
	if len(args) == 0 {
		return "", nil
	}
	if args[0] != "connect" && args[0] != "mdns" && !s.reachable(serial) {
		return "error: device '" + serial + "' not found", fmt.Errorf("exit 1")
	}
	switch args[0] {
	case "connect":
		if s.offline || !s.reachable(args[1]) {
			return "failed to connect to " + args[1], nil
		}
		return "connected to " + args[1], nil
	case "get-state":
		if s.offline {
			return "offline", fmt.Errorf("exit 1")
		}
		if s.dropOnce {
			s.dropOnce = false
			return "offline", fmt.Errorf("exit 1")
		}
		return "device", nil
	case "features":
		return "cmd\nstat_v2", nil
	case "mdns":
		if s.port != "" && s.port != "5555" {
			return "List of discovered mdns services\nadb-sim\t_adb-tls-connect._tcp\t192.168.4.70:" + s.port, nil
		}
		return "", nil
	case "reboot":
		s.reboot()
		return "", nil
	case "push":
		b, _ := os.ReadFile(args[1])
		s.files[args[2]] = b
		return "1 file pushed", nil
	case "pull":
		var b []byte
		switch {
		case strings.HasPrefix(args[1], tvRestoreDir):
			var ok bool
			if b, ok = s.files[args[1]]; !ok {
				return "adb: error: remote object does not exist", fmt.Errorf("exit 1")
			}
		case s.installed:
			b = s.installedAPK
		default:
			return "adb: error: remote object does not exist", fmt.Errorf("exit 1")
		}
		os.WriteFile(args[2], b, 0600)
		return "1 file pulled", nil
	case "install":
		streamed := args[1] != "--no-streaming"
		apk, _ := os.ReadFile(args[len(args)-1])
		if s.streamingBroken && streamed {
			return "Performing Streamed Install\nadb: failed to install " + args[len(args)-1] + ": protocol fault (couldn't read status): Connection reset by peer", fmt.Errorf("exit 1")
		}
		if s.verificationRejectsHome && string(apk) == "payload-standard" {
			return "Performing Push Install\nFailure [INSTALL_FAILED_VERIFICATION_FAILURE]", fmt.Errorf("exit 1")
		}
		s.installed, s.installedAPK = true, apk
		if s.installReplyLost {
			return "adb: failed to install " + args[len(args)-1] + ": ", fmt.Errorf("exit 1")
		}
		return "Performing Streamed Install\nSuccess", nil
	case "shell":
		inner, marked := shellInner(args[1])
		if !marked {
			s.t.Fatalf("%s: shell command without exit-status marker: %s", s.name, args[1])
		}
		out, rc := s.shell(inner)
		reply := out + "\n" + rcMarker + fmt.Sprint(rc)
		if s.crlf {
			reply = strings.ReplaceAll(reply, "\n", "\r\n")
		}
		// No shell_v2: the transport itself always "succeeds".
		return reply, nil
	}
	return "", nil
}

func (s *tvSim) shell(cmd string) (string, int) {
	parts := strings.Split(strings.TrimSuffix(strings.TrimPrefix(cmd, "'"), "'"), "' '")
	has := func(prefix ...string) bool {
		return len(parts) >= len(prefix) && strings.Join(parts[:len(prefix)], "|") == strings.Join(prefix, "|")
	}
	contains := func(v string) bool {
		for _, p := range parts {
			if p == v {
				return true
			}
		}
		return false
	}
	if s.offline {
		return "error: device offline", 1
	}
	if s.sdk < 24 && has("cmd") {
		return "/system/bin/sh: cmd: not found", 127 // Android 5–6 have no cmd binary
	}
	if s.sdk < 23 && has("stat") {
		return "/system/bin/sh: stat: not found", 127
	}
	switch {
	case has("true"):
		return "", 0
	case has("getprop") && len(parts) == 1:
		return fmt.Sprintf("[ro.product.brand]: [%s]\n[ro.product.manufacturer]: [%s]\n[ro.product.model]: [%s]\n[ro.product.device]: [%s]\n[ro.build.version.sdk]: [%d]\n[ro.build.fingerprint]: [%s]\n[ro.build.version.incremental]: [1]\n[ro.crypto.type]: [%s]", s.brand, s.brand, s.model, s.device, s.sdk, s.fingerprint, s.crypto), 0
	case has("getprop", "sys.boot_completed"):
		return "1", 0
	case has("getprop", "persist.vt.use_home_pkg"):
		return s.vendorProp, 0
	case has("getprop"):
		return "", 0
	case has("cat", "/proc/sys/kernel/random/boot_id"):
		if s.noBootID {
			return "cat: /proc/sys/kernel/random/boot_id: Permission denied", 1
		}
		return fmt.Sprintf("b00t-%d", s.bootID), 0
	case has("cat", "/proc/uptime"):
		// Uptime runs with the virtual clock from the last real boot.
		return fmt.Sprintf("%.2f 100.00", s.uptime+(s.clock-s.bootAt).Seconds()), 0
	case has("setprop", "persist.vt.use_home_pkg"):
		s.vendorProp = parts[2]
		return "", 0
	case has("stat"):
		b, ok := s.files[parts[len(parts)-1]]
		if !ok {
			return "stat: No such file or directory", 1
		}
		return fmt.Sprint(len(b)), 0
	case has("settings", "get", "system"):
		if v, ok := s.system[parts[3]]; ok {
			return v, 0
		}
		return "null", 0
	case has("settings", "put", "system"):
		if s.system == nil {
			s.system = map[string]string{}
		}
		s.system[parts[3]] = parts[4]
		return "", 0
	case has("settings", "delete", "system"):
		delete(s.system, parts[3])
		return "Deleted 1 rows", 0
	case has("settings", "get", "secure"):
		v, ok := s.secure[parts[3]]
		if !ok {
			return "null", 0
		}
		return v, 0
	case has("settings", "put", "secure"):
		s.secure[parts[3]] = parts[4]
		return "", 0
	case has("settings", "delete", "secure"):
		delete(s.secure, parts[3])
		return "Deleted 1 rows", 0
	case has("settings", "put", "global"):
		s.global[parts[3]] = parts[4]
		if parts[3] == "adb_enabled" && parts[4] == "0" {
			s.adbOpen = false
		}
		return "", 0
	case has("settings", "get", "global"):
		if parts[3] == "adb_enabled" { // tests reopen debugging by setting adbOpen
			if s.adbOpen {
				return "1", 0
			}
			return "0", 0
		}
		if v, ok := s.global[parts[3]]; ok {
			return v, 0
		}
		return "null", 0
	case has("cmd", "package", "resolve-activity"), has("cmd", "package", "query-activities"):
		if contains("android.intent.category.HOME") && !contains("android.intent.category.DEFAULT") && s.dropPreferredWithoutDefault {
			s.damaged, s.preferred = true, ""
		}
		if s.noBrief && contains("--brief") {
			return "Error: Unknown option: --brief", 255
		}
		full := func(c string) string {
			p, cls := componentPackage(c), strings.SplitN(c, "/", 2)[1]
			if strings.HasPrefix(cls, ".") {
				cls = p + cls
			}
			return "  priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n  ActivityInfo:\n    name=" + cls + "\n    packageName=" + p + "\n    enabled=true exported=true\n"
		}
		if has("cmd", "package", "query-activities") {
			out := ""
			for i, c := range s.candidates() {
				if s.noBrief {
					out += fmt.Sprintf("Activity #%d:\n", i) + full(c)
				} else {
					out += "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n  " + c + "\n"
				}
			}
			return out, 0
		}
		if s.noBrief {
			return full(s.home()), 0
		}
		return "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n" + s.home(), 0
	case has("cmd", "package", "set-home-activity"), has("pm", "set-home-activity"):
		if s.setHomeRefused {
			return "Error: java.lang.SecurityException: set-home-activity not allowed", 255
		}
		s.preferred = parts[len(parts)-1]
		return "Success", 0
	case has("cmd", "role", "add-role-holder"):
		if s.sdk < 29 {
			return "cmd: Can't find service: role", 255
		}
		if s.setHomeRefused {
			return "", 255
		}
		p := parts[len(parts)-1]
		if p == pkg {
			s.preferred = activity
		} else {
			for _, c := range s.vendor {
				if componentPackage(c) == p {
					s.preferred = c
				}
			}
		}
		return "", 0
	case has("cmd", "role"):
		return "", 0
	case has("cmd", "notification"):
		if s.sdk < 26 {
			return "No shell command implementation.", 0
		}
		if len(parts) >= 4 && (parts[2] == "allow_listener" || parts[2] == "disallow_listener") {
			s.secure["enabled_notification_listeners"] = mergeService(s.secure["enabled_notification_listeners"], parts[3], parts[2] == "allow_listener")
		}
		return "", 0
	case has("logcat"):
		if s.xiaomiLog {
			return "10-30 15:44:52.262 D/Settings( 4115): [SettingsBroadcastReceiver]---action = android.intent.action.BOOT_COMPLETED\n10-30 15:44:52.262 D/SettingsUtil( 4115): setSettingsValue  android.provider.Settings$Global.putInt(adb_enabled,0)", 0
		}
		return "", 0
	case has("appops") && s.sdk < 23:
		// Assumed pessimistically: no appops command before Android 6.
		return "/system/bin/sh: appops: not found", 127
	case has("appops", "set") && len(parts) == 5 && parts[2] == pkg:
		if !s.installed {
			return "Error: No such package", 1
		}
		op := parts[3]
		if strings.HasPrefix(op, "android:") {
			op = strings.ToUpper(strings.TrimPrefix(op, "android:")) // public name of the same op
		}
		if parts[4] == "default" {
			delete(s.ops, op)
		} else {
			s.ops[op] = parts[4]
		}
		return "", 0
	case has("appops", "get") && len(parts) == 3 && parts[2] == pkg:
		if !s.installed {
			return "Error: No such package", 1
		}
		if len(s.ops) == 0 {
			return "No operations.", 0
		}
		out := "Uid mode: LEGACY_STORAGE: allow\n"
		for _, op := range []string{"SYSTEM_ALERT_WINDOW", "GET_USAGE_STATS", "APP_AUTO_START", "APP_ASSOC_START", "AUTO_START", "ASSOC_START", "ACCESS_RESTRICTED_SETTINGS"} {
			if m, ok := s.ops[op]; ok {
				out += op + ": " + m + "; time=+3m12s ago\n"
			}
		}
		return out, 0
	case has("dumpsys", "package") && len(parts) == 3:
		if s.noPkgDump {
			return "Permission Denial: can't dump PackageManager", 1
		}
		return s.dumpPackage(), 0
	case has("appops"), has("cmd", "package", "compile"), has("cmd", "package", "list"):
		return "", 0
	case has("pm", "path"):
		if s.installed {
			return "package:/data/app/" + pkg + "-1/base.apk", 0
		}
		return "", 1
	case has("pm", "list", "packages"):
		out := "package:android\n"
		for _, c := range s.vendor {
			out += "package:" + componentPackage(c) + "\n"
		}
		if s.installed {
			out += "package:" + pkg + "\n"
		}
		return out, 0
	case has("pm", "grant"):
		if s.silentGrantFailure {
			return "", 1
		}
		if s.installed {
			s.wss = true
		}
		return "", 0
	case has("pm", "revoke"):
		s.wss = false
		return "", 0
	case has("pm", "uninstall"):
		if s.uninstallFails || !s.installed {
			return "Failure [DELETE_FAILED_INTERNAL_ERROR]", 1
		}
		s.installed, s.installedAPK, s.wss, s.ops, s.keepAdb = false, nil, false, map[string]string{}, false
		if componentPackage(s.preferred) == pkg {
			s.preferred = ""
		}
		if s.front == pkg {
			s.front = componentPackage(s.home())
		}
		return "Success", 0
	case has("am", "start"):
		c := parts[len(parts)-1]
		if componentPackage(c) == pkg && !s.installed {
			return "Error: Activity class {" + c + "} does not exist.", 1
		}
		s.front = componentPackage(c)
		if componentPackage(c) == pkg && s.wss && !s.noSelfRepair {
			// MainActivity.onCreate → GuardSetup.ensureEnabled()
			s.secure["enabled_accessibility_services"] = mergeService(s.secure["enabled_accessibility_services"], guard, true)
			s.secure["accessibility_enabled"] = "1"
		}
		if componentPackage(c) != pkg && s.guardActive() {
			s.front = pkg // the guard takes over whenever a vendor launcher comes to the front
		}
		return "Starting: Intent { cmp=" + c + " }\nStatus: ok\nLaunchState: COLD", 0
	case has("am", "force-stop"):
		if parts[2] == pkg {
			// AOSP AccessibilityManagerService drops a force-stopped app's services from the
			// enabled list (what Xiaomi's standby cleanup triggers too).
			s.secure["enabled_accessibility_services"] = mergeService(s.secure["enabled_accessibility_services"], guard, false)
		}
		if s.front == pkg {
			s.front = componentPackage(s.vendor[0])
		}
		return "", 0
	case has("am", "broadcast"):
		if !s.installed {
			return "Broadcasting: Intent\nBroadcast completed: result=0", 0
		}
		switch {
		case strings.Contains(cmd, "provision_status"):
			return "Broadcast completed: result=200, data=hotel_configured=" + fmt.Sprint(s.secure["sim_server"] == "1") + " pin_configured=" + fmt.Sprint(s.secure["sim_pin"] == "1"), 0
		case strings.Contains(cmd, "provision_rollback"):
			if b, ok := s.provBackup[simExtra(cmd, "provision_rollback")]; ok {
				s.secure["sim_server"], s.provServer, s.provToken = b[0], b[1], b[2]
			}
			return "Broadcast completed: result=200, data=hotel_provisioning_restored", 0
		case strings.Contains(cmd, "staff_pin_sha256"):
			if id := simExtra(cmd, "provision_id"); id != "" {
				if s.provBackup == nil {
					s.provBackup = map[string][3]string{}
				}
				if _, ok := s.provBackup[id]; !ok {
					s.provBackup[id] = [3]string{s.secure["sim_server"], s.provServer, s.provToken}
				}
			}
			s.secure["sim_pin"] = "1"
			if strings.Contains(cmd, "provision_server") {
				s.secure["sim_server"] = "1"
				s.provServer, s.provToken = simExtra(cmd, "provision_server"), simExtra(cmd, "provision_token")
			}
			return "Broadcast completed: result=200, data=hotel_provisioned", 0
		case strings.Contains(cmd, "keep_adb") && !s.noSelfRepair:
			s.keepAdb = strings.Contains(cmd, "'keep_adb' 'true'")
			s.ensureEnabled() // ConfigReceiver ends with GuardSetup.ensureEnabled()
		}
		return "Broadcast completed: result=0", 0
	case has("input", "keyevent"):
		s.pressHome()
		return "", 0
	case has("dumpsys", "activity", "activities"):
		s.tick()
		return s.dumpActivities(), 0
	case has("dumpsys", "activity", "services"):
		if s.guardActive() {
			return "ACTIVITY MANAGER SERVICES (dumpsys activity services)\n  User 0 active services:\n  * ServiceRecord{5e1 u0 com.xjg.hoteltv/.guard.HotelHomeGuardService}", 0
		}
		return "ACTIVITY MANAGER SERVICES (dumpsys activity services)\n  (nothing)", 0
	case has("dumpsys", "window", "windows"):
		return s.dumpWindow(true), 0
	case has("dumpsys", "window"):
		if s.noDumpActivities {
			s.tick()
		}
		return s.dumpWindow(false), 0
	case has("mkdir"), has("rm"), has("df"):
		if has("rm") {
			for k := range s.files {
				if strings.HasPrefix(k, tvRestoreDir) {
					delete(s.files, k)
				}
			}
		}
		return "", 0
	case has("ls"):
		if _, ok := s.files[parts[1]]; ok {
			return parts[1], 0
		}
		return "ls: " + parts[1] + ": No such file or directory", 1
	}
	return "", 0
}

// tick advances the "vendor pulls its launcher back" timer once per foreground read.
func (s *tvSim) tick() {
	if s.retakeCountdown > 0 {
		s.retakeCountdown--
		if s.retakeCountdown == 0 && s.front == pkg {
			s.front = componentPackage(s.vendor[0])
		}
	}
}

func (s *tvSim) called(substr string) bool {
	for _, c := range s.calls {
		if strings.Contains(c, substr) {
			return true
		}
	}
	return false
}

func simEngine(t *testing.T, s *tvSim, verified bool) Engine {
	t.Helper()
	e := Engine{Runner: s, Root: t.TempDir(), State: t.TempDir(), Wait: func(d time.Duration) { s.clock += d; s.advance() }, Catalog: Catalog{Schema: 1, Profiles: []Profile{{ID: "upstream-other", Name: "other", Route: "standard", Match: Match{Brand: "Nobody", Models: []string{"X"}, Min: 28, Max: 28}}}}}
	if verified {
		e.Catalog.Profiles = append(e.Catalog.Profiles, Profile{ID: "hotel-sim", Name: "sim", Route: "standard", Match: Match{Brand: s.brand, Models: []string{s.model}, Devices: []string{s.device}, Min: s.sdk, Max: s.sdk, Fingerprint: s.fingerprint}})
	}
	os.MkdirAll(filepath.Join(e.Root, "payload"), 0700)
	os.WriteFile(filepath.Join(e.Root, "payload", "hotel-standard.apk"), []byte("payload-standard"), 0600)
	os.WriteFile(filepath.Join(e.Root, "payload", "hotel-guard.apk"), []byte("payload-guard"), 0600)
	atomicJSON(filepath.Join(e.Root, "payload", "upgrade-policy.json"), map[string]any{"schema": 1, "allowed_apk_sha256": []string{digest([]byte("payload-standard")), digest([]byte("payload-guard"))}})
	return e
}

func simRequest(trial bool) InstallRequest {
	return InstallRequest{Target: "192.168.4.70", CloseADB: true, AllowTrial: trial}
}

func assertFactory(t *testing.T, s *tvSim) {
	t.Helper()
	if s.installed || componentPackage(s.preferred) == pkg || hasGuard(s.secure["enabled_accessibility_services"]) {
		t.Fatalf("%s: TV not back to its pre-install state: installed=%v preferred=%q a11y=%q", s.name, s.installed, s.preferred, s.secure["enabled_accessibility_services"])
	}
	if s.secure["enabled_accessibility_services"] != "com.voice/com.voice.Reader" {
		t.Fatalf("%s: other accessibility services changed: %q", s.name, s.secure["enabled_accessibility_services"])
	}
	// The real test of durability: another cold boot must not bring the hotel client back.
	s.reboot()
	if s.front == pkg {
		t.Fatalf("%s: hotel client came back after a reboot", s.name)
	}
}

func TestPersonaMatrixInstallRebootAndFactoryRestore(t *testing.T) {
	personas := []func() *tvSim{
		func() *tvSim {
			s := newSim(t, "mitv-astp0-android9")
			s.brand, s.model, s.device = "Xiaomi", "MiTV-ASTP0", "mulan"
			return s
		},
		func() *tvSim {
			s := newSim(t, "gtv-android12")
			s.sdk, s.crypto, s.android12Dumps = 31, "file", true
			return s
		},
		func() *tvSim {
			s := newSim(t, "android14-two-launchers-no-default")
			s.sdk, s.android12Dumps = 34, true
			s.vendor = []string{"com.vendor.tvhome/.MainActivityUserMode", "com.dangbei.launcher/.Main"}
			return s
		},
		func() *tvSim { s := newSim(t, "old-adb-crlf"); s.crlf = true; return s },
		func() *tvSim { s := newSim(t, "android7-no-brief"); s.sdk, s.noBrief = 25, true; return s },
		func() *tvSim { s := newSim(t, "boots-into-live-tv"); s.bootApp = "com.vendor.livetv"; return s },
		func() *tvSim { s := newSim(t, "android6-legacy"); s.sdk = 23; return s },
		func() *tvSim { s := newSim(t, "android5-legacy-crlf"); s.sdk, s.crlf = 22, true; return s },
		func() *tvSim { s := newSim(t, "activity-dump-disabled"); s.noDumpActivities = true; return s },
		func() *tvSim {
			s := newSim(t, "android12-activity-dump-disabled")
			s.sdk, s.android12Dumps, s.noDumpActivities = 31, true, true
			return s
		},
		func() *tvSim { s := newSim(t, "overlay-owns-window-focus"); s.overlayFocus = true; return s },
	}
	for _, mk := range personas {
		for _, verified := range []bool{true, false} {
			s := mk()
			t.Run(fmt.Sprintf("%s/verified=%v", s.name, verified), func(t *testing.T) {
				e := simEngine(t, s, verified)
				var logs []string
				snap, err := e.install(simRequest(!verified), hash(), func(l string) { logs = append(logs, l) })
				if err != nil {
					t.Fatal(err, "\n", strings.Join(logs, "\n"))
				}
				if !snap.RebootVerified || s.front != pkg || !s.installed {
					t.Fatal("cold boot not verified", snap.RebootVerified, s.front)
				}
				if s.adbOpen {
					t.Fatal("ADB close was requested but not sent after a verified reboot")
				}
				if isPlaceholderHome(snap.Home) || componentPackage(snap.Home) != "com.vendor.tvhome" {
					t.Fatal("wrong original HOME recorded", snap.Home)
				}
				if !snap.TVCopy || s.files[tvRestoreDir+"/origin.json"] == nil {
					t.Fatal("restore point not written to the TV")
				}
				if (s.sdk < 24 || (s.sdk >= 29 && s.crypto == "file" && !verified)) != (string(s.installedAPK) == "payload-guard") {
					t.Fatal("route choice wrong for FBE/unverified", string(s.installedAPK))
				}
				s.adbOpen = true // staff re-open debugging from the maintenance menu before a restore
				if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
					t.Fatal("restore:", err)
				}
				if s.files[tvRestoreDir+"/origin.json"] != nil {
					t.Fatal("TV restore point left behind after a full restore")
				}
				assertFactory(t, s)
			})
		}
	}
}

func TestOldStyleRestoreIsUndoneByBootReceiverRevival(t *testing.T) {
	// Negative control: this is what 1.2.0's restore did on a first-install TV (guard removed,
	// HOME handed back, app kept with WRITE_SECURE_SETTINGS). The simulator must show it failing,
	// otherwise the durability test above would prove nothing.
	s := newSim(t, "negative-control")
	e := simEngine(t, s, true)
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	s.secure["enabled_accessibility_services"] = mergeService(s.secure["enabled_accessibility_services"], guard, false)
	s.preferred = s.vendor[0]
	s.front = componentPackage(s.vendor[0])
	s.reboot()
	if s.front != pkg {
		t.Fatal("simulator does not model the revival that made the 1.2.0 restore non-durable")
	}
}

func TestBackupNeverQueriesHomeWithoutDefault(t *testing.T) {
	s := newSim(t, "xgimi-fallbackhome-without-default")
	s.dropPreferredWithoutDefault = true
	s.vendor = []string{"com.xgimi.home/.Launcher", "com.vendor.tvhome/.MainActivityUserMode"}
	s.preferred = "com.xgimi.home/.Launcher"
	e := simEngine(t, s, true)
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if s.damaged {
		t.Fatal("a HOME query without CATEGORY_DEFAULT dropped the TV's default launcher")
	}
}

func TestChooserIsNeverRecordedAsOriginalHome(t *testing.T) {
	s := newSim(t, "chooser")
	// Some ROMs list their FallbackHome with CATEGORY_DEFAULT, and first.
	s.vendor = []string{"com.android.tv.settings/.system.FallbackHome", "com.vendor.tvhome/.MainActivityUserMode", "com.other.launcher/.Main"}
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if snap.HomeResolved != "" || snap.Home != "com.vendor.tvhome/.MainActivityUserMode" {
		t.Fatal("chooser recorded as HOME", snap.HomeResolved, snap.Home)
	}
}

func TestVendorTakingHomeBackFailsAndRestores(t *testing.T) {
	s := newSim(t, "vendor-retakes-home")
	s.vendorRetakeAfter = 2
	e := simEngine(t, s, true)
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "未稳定回到影院") || !strings.Contains(err.Error(), "已执行恢复") {
		t.Fatal("a launcher pulled back after seconds passed the HOME test", err)
	}
	assertFactory(t, s)
}

func TestColdBootLossRestoresWhileADBStillOpen(t *testing.T) {
	s := newSim(t, "boot-loses-home")
	s.rebootLosesHome = true
	e := simEngine(t, s, true)
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "软件重启后") {
		t.Fatal(err)
	}
	if !s.adbOpen || s.called("adb_enabled' '0'") {
		t.Fatal("ADB was closed before the cold boot check passed")
	}
	s.rebootLosesHome = false
	assertFactory(t, s)
}

func TestRebootUnreachableKeepsBackupAndNeverClaimsSuccess(t *testing.T) {
	// Debugging survives the pre-check reboot but is lost at the verification boot.
	s := newSim(t, "unreachable-after-reboot")
	s.unreachableFromBoot = 2
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if !snap.RebootUnreachable || snap.RebootVerified || snap.ADBCloseRequested {
		t.Fatal("unreachable reboot reported as verified", snap)
	}
	b, _ := os.ReadFile(filepath.Join(e.State, "field-records.jsonl"))
	if !strings.Contains(string(b), `"result":"reboot_unverified"`) {
		t.Fatal("field record missing", string(b))
	}
}

func TestTCLVerificationFailureFallsBackToGuardBuild(t *testing.T) {
	s := newSim(t, "tcl-verifier")
	s.verificationRejectsHome = true
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if snap.Route != "guard" || string(s.installedAPK) != "payload-guard" {
		t.Fatal("did not fall back to guard", snap.Route)
	}
}

func TestStreamedInstallFailureRetriesWithoutStreaming(t *testing.T) {
	s := newSim(t, "streaming-broken")
	s.streamingBroken = true
	e := simEngine(t, s, true)
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if !s.called("install --no-streaming") {
		t.Fatal("no --no-streaming retry")
	}
}

func TestGuardThatNeverBindsStopsAndRestores(t *testing.T) {
	s := newSim(t, "guard-unbound")
	s.guardNeverBinds = true
	e := simEngine(t, s, true)
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "没有绑定") {
		t.Fatal(err)
	}
	assertFactory(t, s)
}

func TestUnverifiedModelNeedsOptInBeforeAnyChange(t *testing.T) {
	s := newSim(t, "unknown-fbe")
	s.sdk, s.crypto, s.android12Dumps = 30, "file", true
	e := simEngine(t, s, false)
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err == nil || !strings.Contains(err.Error(), "允许试装") {
		t.Fatal(err)
	}
	for _, c := range s.calls {
		if strings.HasPrefix(c, "install") || strings.Contains(c, "'put'") || strings.HasPrefix(c, "push") {
			t.Fatal("changed the TV without opt-in:", c)
		}
	}
	req := simRequest(true)
	req.SkipRebootCheck = true // must be ignored for a trial
	snap, err := e.install(req, hash(), func(string) {})
	if err != nil || !snap.RebootVerified || string(s.installedAPK) != "payload-guard" {
		t.Fatal(err, snap.RebootVerified, string(s.installedAPK))
	}
}

func TestReinstallSnapshotFallsBackToFirstRecord(t *testing.T) {
	s := newSim(t, "re-run")
	e := simEngine(t, s, true)
	first, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	second, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if !second.Polluted || second.Origin != first.ID || !second.Installed {
		t.Fatal("second snapshot not linked to the first", second.Polluted, second.Origin, first.ID)
	}
	s.adbOpen = true
	if err = e.restore(second, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal(err)
	}
	assertFactory(t, s)
}

func TestRestorePointOnTVWorksFromAnotherPC(t *testing.T) {
	s := newSim(t, "other-pc")
	a := simEngine(t, s, true)
	if _, err := a.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	b := simEngine(t, s, true) // empty data/ on a second installer computer
	snap, err := b.importTVRestorePoint("192.168.4.70")
	if err != nil {
		t.Fatal(err)
	}
	if !snap.FromTV {
		t.Fatal("not marked as imported")
	}
	if err = b.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal(err)
	}
	assertFactory(t, s)
	other := newSim(t, "different-tv")
	other.files = s.files
	if _, err = simEngine(t, other, true).importTVRestorePoint("192.168.4.71"); err == nil {
		t.Fatal("imported another TV's restore point")
	}
}

func TestSnapshotWrittenBy120StillRestoresToFactory(t *testing.T) {
	// Shape of the real 1.2.0 backup of the MiTV-ASTP0 installed on 2026-10-29 (identity replaced).
	s := newSim(t, "astp0-120-backup")
	s.brand, s.model, s.device = "Xiaomi", "MiTV-ASTP0", "mulan"
	s.vendor = []string{"com.mitv.tvhome/.MainActivityUserMode"}
	e := simEngine(t, s, true)
	if _, err := e.install(InstallRequest{Target: "192.168.4.70", SkipRebootCheck: true}, hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	id := strings.Repeat("e", 32)
	old := map[string]any{"id": id, "target": "192.168.4.70:5555", "at": "2026-10-29T22:38:07+08:00",
		"facts": map[string]any{"brand": "Xiaomi", "model": "MiTV-ASTP0", "device": "mulan", "sdk": 28, "firmware": "1", "fingerprint": s.fingerprint},
		"home":  "com.mitv.tvhome/.MainActivityUserMode", "accessibility": "com.voice/com.voice.Reader", "accessibility_enabled": "1",
		"notification": "null", "vendor_home": "", "installed": false, "apks": nil, "apk_hashes": map[string]string{},
		"identity": digest([]byte(s.identity)), "provision_attempted": true}
	b, _ := json.Marshal(old)
	os.MkdirAll(filepath.Join(e.State, "backups", id), 0700)
	os.WriteFile(filepath.Join(e.State, "backups", id, "snapshot.json"), b, 0600)
	snap, err := e.loadSnapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal(err)
	}
	assertFactory(t, s)
}

func TestUninstallFailureIsReportedNotHidden(t *testing.T) {
	s := newSim(t, "uninstall-fails")
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.uninstallFails = true
	if err = e.restore(snap); err == nil || !strings.Contains(err.Error(), "卸载失败") {
		t.Fatal("restore claimed success although the client is still installed", err)
	}
	if s.wss {
		t.Fatal("WRITE_SECURE_SETTINGS kept on a client that could not be removed: its guard would return at the next boot")
	}
	s.reboot()
	// The left-over client may still open itself at boot (BootReceiver, Android ≤ 9); what must not
	// come back is its guard, i.e. HOME.
	if hasGuard(s.secure["enabled_accessibility_services"]) {
		t.Fatal("left-over client switched its guard back on after a reboot")
	}
	s.pressHome()
	if s.front == pkg {
		t.Fatal("left-over client took HOME back after a reboot")
	}
}

func TestBelowAndroid5IsBlockedBeforeTouchingTV(t *testing.T) {
	s := newSim(t, "android44")
	s.sdk = 19
	e := simEngine(t, s, false)
	if _, err := e.install(simRequest(true), hash(), func(string) {}); err == nil || !strings.Contains(err.Error(), "Android 5.0") {
		t.Fatal(err)
	}
	for _, c := range s.calls {
		if strings.HasPrefix(c, "install") || strings.Contains(c, "'put'") || strings.HasPrefix(c, "push") || strings.HasPrefix(c, "pull") {
			t.Fatal(c)
		}
	}
}

func TestDiagnoseIsReadOnlyAndCarriesNoIdentity(t *testing.T) {
	s := newSim(t, "diagnose")
	s.sdk, s.crypto, s.android12Dumps = 31, "file", true
	e := simEngine(t, s, false)
	d, err := e.diagnose("192.168.4.70")
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range s.calls {
		for _, w := range []string{"'put'", "'delete'", "'grant'", "'revoke'", "'uninstall'", "set-home-activity", "add-role-holder", "'keyevent'", "'force-stop'", "'broadcast'"} {
			if strings.Contains(c, w) {
				t.Fatal("diagnose changed the TV:", c)
			}
		}
		if strings.HasPrefix(c, "install") || strings.HasPrefix(c, "push") || c == "reboot" {
			t.Fatal("diagnose changed the TV:", c)
		}
	}
	b, _ := json.Marshal(d)
	if strings.Contains(string(b), s.identity) || strings.Contains(string(b), "192.168.4.70") {
		t.Fatal("diagnosis leaks identity or address")
	}
	if d.Checks["foreground"] != "ok" || d.Checks["home_query"] != "ok" || d.Foreground == "" || len(d.Risks) == 0 {
		t.Fatal(d.Checks, d.Foreground, d.Risks)
	}
}

func TestRefusalDetectionIsCaseInsensitive(t *testing.T) {
	for _, out := range []string{"Security exception: Permission Denial: not allowed", "java.lang.SecurityException: x", "Error: Unknown option: --brief", "cmd: Can't find service: role", "Failure [INSTALL_FAILED_ABORTED]", "/system/bin/sh: cmd: not found"} {
		if !refusal(out) {
			t.Fatal("missed", out)
		}
	}
	for _, out := range []string{"Status: ok", "Success", "", "Broadcast completed: result=200, data=hotel_provisioned"} {
		if refusal(out) {
			t.Fatal("false refusal", out)
		}
	}
}

func TestForegroundParsersAcrossVersions(t *testing.T) {
	for dump, want := range map[string]string{
		"    mResumedActivity: ActivityRecord{c2e4b2d u0 com.mitv.tvhome/.MainActivityUserMode t2}":                                                                        "com.mitv.tvhome",
		"  topResumedActivity=ActivityRecord{9f1 u0 com.google.android.apps.tv.launcherx/.home.HomeActivity t31}\n  ResumedActivity: ActivityRecord{x u0 other.pkg/.A t1}": "com.google.android.apps.tv.launcherx",
		"Display #0\n  ResumedActivity: ActivityRecord{9f1 u0 com.tcl.launcher/.Main t3}":                                                                                  "com.tcl.launcher",
	} {
		if got := resumedPackage(dump); got != want {
			t.Fatal(got, want)
		}
	}
	if m := focusRe.FindStringSubmatch("  mCurrentFocus=Window{7d1 u0 com.xjg.hoteltv/com.xjg.hoteltv.MainActivity}"); m == nil || m[1] != pkg {
		t.Fatal(m)
	}
	if homeComponent("priority=0 preferredOrder=0\nActivityInfo:\n  name=com.android.launcher3.Launcher\n  packageName=com.android.launcher3\n") != "com.android.launcher3/.Launcher" {
		t.Fatal("Android 7 full-dump fallback")
	}
}

func TestInstallFailureCodesBecomeStaffAdvice(t *testing.T) {
	for out, want := range map[string]string{
		"Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]":                                 "存储空间不足",
		"Failure [INSTALL_FAILED_USER_RESTRICTED: Install canceled by user]":            "通过USB安装",
		"Failure [INSTALL_FAILED_VERIFICATION_FAILURE]":                                 "安装校验",
		"Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: Package signatures do not match]": "签名不同",
		"Failure [INSTALL_PARSE_FAILED_NO_CERTIFICATES]":                                "无法解析",
	} {
		if _, advice := installAdvice(out); !strings.Contains(advice, want) {
			t.Fatal(out, advice)
		}
	}
	if streamingProblem("Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]") || !streamingProblem("adb: failed to install x.apk: protocol fault (couldn't read status): Connection reset by peer") {
		t.Fatal("streaming classification")
	}
}

func TestAdvisoryWarnsOrBlocks(t *testing.T) {
	c := Catalog{Schema: 1, Advisories: []Advisory{{Brand: "Skyworth", SDKMin: 33, SDKMax: 33, ModelsContains: []string{"7T641"}, Level: "warn", Note: "黑屏风险", Source: "Aerial #864"}, {Brand: "FFALCON", Devices: []string{"t966d5"}, Level: "block", Note: "约每分钟清掉无障碍", Source: "Aerial ffalcon-t966d5.md"}}}
	d := decide(c, Facts{Brand: "Skyworth", Model: "Skyworth 7T641", SDK: 33})
	if !d.Trial || len(d.Warnings) == 0 || !strings.Contains(strings.Join(d.Warnings, ""), "黑屏") {
		t.Fatal(d)
	}
	if d = decide(c, Facts{Brand: "FFALCON", Model: "75S78A", Device: "t966d5", SDK: 34}); d.Trial || d.Allowed || d.Tier != "blocked" {
		t.Fatal(d)
	}
}

func TestSilentNonZeroExitIsAFailure(t *testing.T) {
	// Synthetic: a refusal that prints nothing. Only the TV-side exit status reveals it, and the
	// transport here never reports one (pre-shell_v2 behaviour).
	s := newSim(t, "silent-grant-failure")
	s.silentGrantFailure = true
	e := simEngine(t, s, true)
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "必要的守护权限") {
		t.Fatal("a non-zero exit status without text was treated as success", err)
	}
	assertFactory(t, s)
}

// ---- Regression tests for the independent review of 1.3.0 (one per finding) ----

func allowAPK(e Engine, extra ...[]byte) {
	hashes := []string{digest([]byte("payload-standard")), digest([]byte("payload-guard"))}
	for _, b := range extra {
		hashes = append(hashes, digest(b))
	}
	atomicJSON(filepath.Join(e.Root, "payload", "upgrade-policy.json"), map[string]any{"schema": 1, "allowed_apk_sha256": hashes})
}

func TestRestoreNeverPassesOnTheChooser(t *testing.T) {
	// Finding 1: the TV had an explicit default; restore cannot set it back and the TV ends on the chooser.
	s := newSim(t, "explicit-default-refused-later")
	s.vendor = []string{"com.vendor.tvhome/.MainActivityUserMode", "com.dangbei.launcher/.Main"}
	s.preferred = s.vendor[0]
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.setHomeRefused = true
	// Without the reboot check: the post-restore HOME key test alone must catch it.
	err = e.restore(snap)
	if err == nil {
		t.Fatal("restore reported success although HOME now opens the chooser", s.front, s.preferred)
	}
}

func TestRestoreToChooserWhenThatWasTheOriginal(t *testing.T) {
	s := newSim(t, "chooser-original")
	s.vendor = []string{"com.vendor.tvhome/.MainActivityUserMode", "com.dangbei.launcher/.Main"}
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.setHomeRefused, s.adbOpen = true, true
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal("the TV is back to its original chooser state:", err)
	}
	if s.installed {
		t.Fatal("client left installed")
	}
}

func TestFailedReRunRollsBackToPreRunStateNotFactory(t *testing.T) {
	// Finding 3: a re-run that fails must not uninstall a working client and its data.
	s := newSim(t, "rerun-fails")
	e := simEngine(t, s, true)
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	s.silentGrantFailure = true // a transient refusal half-way through the re-run
	_, err := e.install(simRequest(false), hash(), func(string) {})
	s.silentGrantFailure = false
	if err == nil || !strings.Contains(err.Error(), "已执行恢复") {
		t.Fatal(err)
	}
	if !s.installed || !hasGuard(s.secure["enabled_accessibility_services"]) || s.secure["sim_pin"] != "1" {
		t.Fatal("automatic rollback wiped the working client", s.installed, s.secure)
	}
}

func TestReinstallOnAnotherPCUsesTVOriginAndVendorProperty(t *testing.T) {
	// Finding 2: polluted record from a second PC; the vendor HOME property must not stay on our package.
	for _, keepTVOrigin := range []bool{true, false} {
		s := newSim(t, fmt.Sprintf("vendor-prop-tvorigin-%v", keepTVOrigin))
		s.vendorProp = "com.vendor.tvhome"
		a := simEngine(t, s, true)
		if _, err := a.install(simRequest(false), hash(), func(string) {}); err != nil {
			t.Fatal(err)
		}
		if s.vendorProp != pkg {
			t.Fatal("standard route did not set the vendor property", s.vendorProp)
		}
		if !keepTVOrigin {
			delete(s.files, tvRestoreDir+"/origin.json")
		}
		s.adbOpen = true
		b := simEngine(t, s, true)
		second, err := b.install(simRequest(false), hash(), func(string) {})
		if err != nil {
			t.Fatal(err)
		}
		if !second.Polluted || second.Origin != "" || second.VendorHome != pkg {
			t.Fatal("expected a polluted record without local origin", second.Polluted, second.Origin, second.VendorHome)
		}
		s.adbOpen = true
		if err = b.restore(second, RestoreOptions{RebootCheck: true}); err != nil {
			t.Fatal(err)
		}
		if s.vendorProp != "com.vendor.tvhome" {
			t.Fatal("vendor HOME property left on the uninstalled client:", s.vendorProp)
		}
		assertFactory(t, s)
	}
}

func TestWirelessPortChangeDuringInstallAndRestoreAtNewAddress(t *testing.T) {
	// Finding 4: new debugging port after reboot; later restore with an operator-given address.
	s := newSim(t, "wireless-port")
	s.sdk, s.android12Dumps, s.port, s.portAfterReboot = 31, true, "37001", "40123"
	e := simEngine(t, s, true)
	req := simRequest(false)
	req.Target = "192.168.4.70:37001"
	snap, err := e.install(req, hash(), func(string) {})
	if err != nil || !snap.RebootVerified || snap.Target != "192.168.4.70:40123" {
		t.Fatal(err, snap.RebootVerified, snap.Target)
	}
	s.adbOpen, s.port = true, "40500" // another boot later, another port
	if err = e.restore(snap); err == nil {
		t.Fatal("restore reached a port that no longer exists")
	}
	if err = e.restore(snap, RestoreOptions{Target: "192.168.4.70:40500"}); err != nil {
		t.Fatal(err)
	}
	if s.installed {
		t.Fatal("not restored")
	}
	other := newSim(t, "wrong-tv-at-address")
	if err = simEngine(t, other, true).restore(snap, RestoreOptions{Target: "192.168.4.70:5555"}); err == nil || !strings.Contains(err.Error(), "身份") {
		t.Fatal("an address override reached another TV", err)
	}
}

func TestFirmwareUpdateDoesNotBlockRestore(t *testing.T) {
	s := newSim(t, "ota")
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.fingerprint = "test/fingerprint-ota2"
	var logs []string
	if err = e.restore(snap, RestoreOptions{Log: func(l string) { logs = append(logs, l) }}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(logs, "\n"), "固件与备份时不同") {
		t.Fatal("firmware change not reported")
	}
}

func TestUpgradeRestoreOnStreamingBrokenTVKeepsGuardedPilot(t *testing.T) {
	// Findings 5 and 6: reinstalling the older client must use the --no-streaming retry, and a
	// restored state that keeps the client's guard expects the client in front, not the vendor.
	s := newSim(t, "pilot-guard-upgrade")
	pilot := []byte("pilot-guard-apk")
	s.installed, s.installedAPK, s.wss = true, pilot, true
	s.secure["enabled_accessibility_services"] = "com.voice/com.voice.Reader:" + guard
	s.secure["sim_pin"] = "1"
	s.front = pkg
	s.streamingBroken = true
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if snap.Polluted || !snap.Installed {
		t.Fatal("pilot record must be a clean upgrade record", snap.Polluted)
	}
	s.adbOpen = true
	if err = e.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal(err)
	}
	if string(s.installedAPK) != string(pilot) || !hasGuard(s.secure["enabled_accessibility_services"]) || s.front != pkg {
		t.Fatal("pilot client not restored as it was", string(s.installedAPK), s.front)
	}
}

func TestForceStopDroppingGuardIsRepairedBeforeSuccess(t *testing.T) {
	// Finding 7: the post-provisioning force-stop removes the guard; nothing may report success
	// while it is gone, even with the reboot check off on a verified model.
	s := newSim(t, "no-self-repair")
	s.noSelfRepair = true
	e := simEngine(t, s, true)
	req := simRequest(false)
	req.SkipRebootCheck = true
	if _, err := e.install(req, hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if !hasGuard(s.secure["enabled_accessibility_services"]) {
		t.Fatal("install finished with the guard switched off")
	}
	s.pressHome()
	if s.front != pkg {
		t.Fatal("HOME does not reach the client after install")
	}
}

func TestTVRestorePointIsUntrustedInput(t *testing.T) {
	// Finding 9.
	s := newSim(t, "forged-restore-point")
	e := simEngine(t, s, true)
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	var good map[string]any
	json.Unmarshal(s.files[tvRestoreDir+"/origin.json"], &good)
	forge := func(k string, v any) {
		m := map[string]any{}
		for kk, vv := range good {
			m[kk] = vv
		}
		m[k] = v
		b, _ := json.Marshal(m)
		s.files[tvRestoreDir+"/origin.json"] = b
	}
	for _, c := range []struct {
		k string
		v any
	}{{"home", "com.evil.app/.Home"}, {"vendor_home", "x; reboot"}, {"home", pkg + "/.MainActivity"}} {
		forge(c.k, c.v)
		if _, err := simEngine(t, s, true).importTVRestorePoint("192.168.4.70"); err == nil {
			t.Fatal("forged restore point accepted:", c.k, c.v)
		}
	}
	forge("polluted", true)
	b := simEngine(t, s, true)
	got, err := b.importTVRestorePoint("192.168.4.70")
	if err != nil || got.Polluted || got.Origin != "" || !got.FromTV {
		t.Fatal("untrusted fields carried over", err, got.Polluted, got.Origin)
	}
	s.files[tvRestoreDir+"/origin.json"] = make([]byte, 300<<10)
	if _, err = simEngine(t, s, true).importTVRestorePoint("192.168.4.70"); err == nil || !strings.Contains(err.Error(), "大小") {
		t.Fatal("oversized restore point not refused before pulling", err)
	}
}

func TestIgnoredRebootIsNotTakenForABoot(t *testing.T) {
	// Finding 10: no boot_id and a reboot that never happened must not pass.
	// Since 1.3.2 every TV gets the pre-check reboot, so a reboot that never happens is caught there,
	// before anything changes.
	s := newSim(t, "no-boot-id")
	s.noBootID, s.ignoreReboot = true, true
	e := simEngine(t, s, true)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "没有改动电视") || snap.RebootVerified || s.installed || !s.adbOpen {
		t.Fatal("a reboot that did not happen was accepted", err, snap.RebootVerified)
	}
	s2 := newSim(t, "no-boot-id-real-reboot")
	s2.noBootID = true
	snap, err = simEngine(t, s2, true).install(simRequest(false), hash(), func(string) {})
	if err != nil || !snap.RebootVerified {
		t.Fatal("real reboot without boot_id not recognised", err)
	}
}

type stderrAfterMarker struct{ out string }

func (r stderrAfterMarker) Run(serial string, args ...string) (string, error) { return r.out, nil }

func TestShellKeepsStderrThatArrivesAfterTheMarker(t *testing.T) {
	// Finding 8: with shell_v2 stderr is a separate stream.
	r := stderrAfterMarker{"\n" + rcMarker + "0\nError while accessing settings provider\n"}
	if _, err := shell(r, "x", "settings", "put", "secure", "a", "b"); err == nil {
		t.Fatal("refusal printed after the marker was dropped")
	}
	if !refusal("No shell command implementation.") {
		t.Fatal("Android 7 cmd stub not treated as refusal")
	}
}

func TestAndroid7UpgradeRestoreUsesSettingsForNotificationListener(t *testing.T) {
	s := newSim(t, "android7-upgrade")
	s.sdk = 25
	pilot := []byte("pilot-standard-apk")
	s.installed, s.installedAPK, s.wss = true, pilot, true
	s.secure["enabled_notification_listeners"] = listener
	e := simEngine(t, s, true)
	allowAPK(e, pilot)
	snap, err := e.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	if err = e.restore(snap); err != nil {
		t.Fatal("Android 7 restore failed on the notification step:", err)
	}
	if !hasListener(s.secure["enabled_notification_listeners"]) {
		t.Fatal("listener binding not restored")
	}
}

func TestRestoreAPIRejectsBadTargetOverride(t *testing.T) {
	a := newTestApp(t)
	id := strings.Repeat("f", 32)
	atomicJSON(filepath.Join(a.Engine.State, "backups", id, "snapshot.json"), Snapshot{ID: id, Target: "192.168.4.62:5555", Home: "com.vendor/.Home"})
	w := call(a, "/api/restore", `{"id":"`+id+`","reboot_check":true,"target":"8.8.8.8"}`, a.Token, "", a.Host)
	if w.Code != 400 || a.job != nil {
		t.Fatal(w.Code, w.Body.String())
	}
}

func TestAndroid7RestorePointOnTVIsUsable(t *testing.T) {
	// Re-check finding 1: without --brief the candidate list must still be read.
	s := newSim(t, "android7-tv-origin")
	s.sdk, s.noBrief = 25, true
	s.vendor = []string{"com.vendor.tvhome/.MainActivityUserMode", "com.dangbei.launcher/.Main"}
	s.preferred = s.vendor[0]
	a := simEngine(t, s, true)
	first, err := a.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	if len(first.HomeCandidates) < 2 || first.Home != s.vendor[0] {
		t.Fatal("Android 7 HOME candidates not read", first.HomeCandidates, first.Home)
	}
	s.adbOpen = true
	if _, err = simEngine(t, s, true).importTVRestorePoint("192.168.4.70"); err != nil {
		t.Fatal("import on Android 7:", err)
	}
	b := simEngine(t, s, true)
	second, err := b.install(simRequest(false), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	if err = b.restore(second, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal("second-PC restore on Android 7:", err)
	}
	assertFactory(t, s)
}

func TestRestoreTargetUpdatedAfterRebootPortChange(t *testing.T) {
	// Re-check finding 3: the address the TV came back on is reported and the TV copy is removed there.
	s := newSim(t, "restore-port-change")
	s.sdk, s.android12Dumps, s.port = 31, true, "37001"
	e := simEngine(t, s, true)
	req := simRequest(false)
	req.Target = "192.168.4.70:37001"
	req.SkipRebootCheck = true
	snap, err := e.install(req, hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	s.portAfterReboot, s.adbOpen = "41000", true
	moved := ""
	if err = e.restore(snap, RestoreOptions{RebootCheck: true, Moved: func(t string) { moved = t }}); err != nil {
		t.Fatal(err)
	}
	if moved != "192.168.4.70:41000" || s.files[tvRestoreDir+"/origin.json"] != nil {
		t.Fatal("new address not reported or TV restore point left", moved)
	}
}

// ---- Grading of field evidence from elsewhere (Aerial) ----

func gradedEngine(t *testing.T, s *tvSim, grade string) Engine {
	e := simEngine(t, s, false)
	e.Catalog.Profiles = append(e.Catalog.Profiles, Profile{ID: "aerial-sim", Name: "aerial", Route: "standard", Grade: grade, Evidence: "https://github.com/pengchujin/aerial-tv/issues/0", Notes: "sim evidence", Match: Match{Models: []string{s.model}, Devices: []string{s.device}, Min: s.sdk, Max: s.sdk}})
	return e
}

func TestProvenModelInstallsWithoutOptInButWithBootCheckAndObservation(t *testing.T) {
	s := newSim(t, "proven")
	e := gradedEngine(t, s, "proven")
	var logs []string
	req := simRequest(false)
	req.SkipRebootCheck = true // ignored outside this project's own verified firmwares
	snap, err := e.install(req, hash(), func(l string) { logs = append(logs, l) })
	if err != nil {
		t.Fatal(err)
	}
	if snap.Tier != "proven" || !snap.RebootVerified || !strings.Contains(strings.Join(logs, "\n"), "稳定观察通过") {
		t.Fatal(snap.Tier, snap.RebootVerified, logs)
	}
}

func TestGuardClearedAfterBootIsCaughtByObservation(t *testing.T) {
	// TCL T966D5: the firmware clears accessibility about every 52 s; the client repairs the
	// setting within 1.5 s, but from then on HOME stays on the vendor launcher. A boot check
	// alone passes; the HOME rounds of the observation must not.
	s := newSim(t, "t966d5-like")
	s.clearEvery, s.repairDelay, s.homeBrokenAfterClear = 52*time.Second, 1500*time.Millisecond, true
	s.clearFromBoot = 2 // boot 1 is the ADB pre-check reboot; the clearing starts on the verification boot
	e := gradedEngine(t, s, "proven")
	_, err := e.install(simRequest(false), hash(), func(string) {})
	// Since 1.3.4 the boot settles for about a minute first, so the first clear may already be
	// caught by the boot check rather than by the observation: either way it must fail and restore.
	if err == nil || !(strings.Contains(err.Error(), "稳定观察") || strings.Contains(err.Error(), "软件重启后")) || !strings.Contains(err.Error(), "已执行恢复") {
		t.Fatal("a TV that loses HOME after the first clear was accepted", err)
	}
	s.clearEvery, s.homeBrokenAfterClear = 0, false
	assertFactory(t, s)
}

func TestLateClearIsCaughtByTheObservationHomeRounds(t *testing.T) {
	// The first clear comes after the boot check (a slower cycle than the T966D5's): only the HOME
	// rounds of the two-minute observation can see that HOME stays on the vendor launcher.
	s := newSim(t, "late-clear")
	s.clearEvery, s.repairDelay, s.homeBrokenAfterClear = 150*time.Second, 1500*time.Millisecond, true
	s.clearFromBoot = 2
	e := gradedEngine(t, s, "proven")
	_, err := e.install(simRequest(false), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "稳定观察") || !strings.Contains(err.Error(), "已执行恢复") {
		t.Fatal("HOME lost after a late clear was not caught by the observation", err)
	}
}

func TestGuardClearedForGoodIsCaughtFromSettings(t *testing.T) {
	s := newSim(t, "guard-cleared-for-good")
	s.clearEvery = 52 * time.Second // no repair
	e := gradedEngine(t, s, "proven")
	_, err := e.install(simRequest(false), hash(), func(string) {})
	// Wherever the clearing is first seen (binding check, boot check, observation), it must fail.
	if err == nil || !(strings.Contains(err.Error(), "守护已被系统关闭") || strings.Contains(err.Error(), "没有绑定") || strings.Contains(err.Error(), "软件重启后")) || !strings.Contains(err.Error(), "已执行恢复") {
		t.Fatal(err)
	}
}

func TestBriefRepairedClearsDoNotFailAHealthyTV(t *testing.T) {
	// No false failure: the firmware clears now and then, the client repairs within 1.5 s and
	// HOME keeps working.
	s := newSim(t, "repaired-clears")
	s.clearEvery, s.repairDelay = 52*time.Second, 1500*time.Millisecond
	e := gradedEngine(t, s, "proven")
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal("healthy TV failed the observation:", err)
	}
}

func TestTrialRefusedWhenADBDoesNotSurviveAReboot(t *testing.T) {
	// Without this project's own record the automatic restore needs ADB after a reboot; prove it first.
	s := newSim(t, "adb-lost-on-reboot")
	s.rebootUnreachable = true
	e := simEngine(t, s, false)
	_, err := e.install(simRequest(true), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "没有改动电视") {
		t.Fatal(err)
	}
	for _, c := range s.calls {
		if strings.HasPrefix(c, "install") || strings.Contains(c, "'put'") || strings.HasPrefix(c, "push") {
			t.Fatal("changed the TV although restore would be impossible:", c)
		}
	}
}

func TestAvoidGradeNeedsOptInAndUsesGuard(t *testing.T) {
	s := newSim(t, "avoid")
	e := gradedEngine(t, s, "avoid")
	if d := decide(e.Catalog, Facts{Model: s.model, Device: s.device, SDK: s.sdk}); d.Tier != "avoid" || !d.Avoid || d.Allowed || !d.Trial || d.Route != "guard" {
		t.Fatal(d)
	}
	if _, err := e.install(simRequest(false), hash(), func(string) {}); err == nil || !strings.Contains(err.Error(), "允许试装") {
		t.Fatal(err)
	}
	if _, err := e.install(simRequest(true), hash(), func(string) {}); err == nil || !strings.Contains(err.Error(), "再次确认") {
		t.Fatal("avoid tier installed without the second confirmation", err)
	}
	req := simRequest(true)
	req.AllowAvoid = true
	snap, err := e.install(req, hash(), func(string) {})
	if err != nil || string(s.installedAPK) != "payload-guard" || !snap.RebootVerified {
		t.Fatal(err, string(s.installedAPK))
	}
}

func TestConflictingEvidenceIsATrial(t *testing.T) {
	s := newSim(t, "conflict")
	e := gradedEngine(t, s, "proven")
	e.Catalog.Profiles = append(e.Catalog.Profiles, Profile{ID: "aerial-sim-fail", Route: "guard", Grade: "avoid", Notes: "failed elsewhere", Match: Match{Models: []string{s.model}, Devices: []string{s.device}, Min: s.sdk, Max: s.sdk}})
	d := decide(e.Catalog, Facts{Model: s.model, Device: s.device, SDK: s.sdk})
	if d.Allowed || !d.Trial || d.Tier != "trial" || len(d.Warnings) < 2 {
		t.Fatal(d)
	}
}

func TestAndroid6LegacyRestoreFromTVPointAndUpgrade(t *testing.T) {
	// Android 6: no cmd; restore point import and an upgrade restore must still work.
	s := newSim(t, "android6-more")
	s.sdk = 23
	a := simEngine(t, s, false)
	if _, err := a.install(simRequest(true), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if s.preferred != "" {
		t.Fatal("the installer changed the default HOME on Android 6", s.preferred)
	}
	s.adbOpen = true
	b := simEngine(t, s, false)
	snap, err := b.importTVRestorePoint("192.168.4.70")
	if err != nil {
		t.Fatal(err)
	}
	if err = b.restore(snap, RestoreOptions{RebootCheck: true}); err != nil {
		t.Fatal(err)
	}
	assertFactory(t, s)
}

// simExtra reads one "--es name value" pair from a quoted shell command.
func simExtra(cmd, name string) string {
	i := strings.Index(cmd, "'"+name+"' '")
	if i < 0 {
		return ""
	}
	rest := cmd[i+len(name)+4:]
	if j := strings.Index(rest, "'"); j >= 0 {
		return rest[:j]
	}
	return ""
}
