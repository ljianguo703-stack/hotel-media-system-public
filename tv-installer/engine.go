package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type Snapshot struct {
	ID                   string            `json:"id"`
	Target               string            `json:"target"`
	At                   string            `json:"at"`
	Facts                Facts             `json:"facts"`
	Home                 string            `json:"home"`
	Accessibility        string            `json:"accessibility"`
	AccessibilityEnabled string            `json:"accessibility_enabled"`
	Notification         string            `json:"notification"`
	VendorHome           string            `json:"vendor_home"`
	Installed            bool              `json:"installed"`
	APKs                 []string          `json:"apks"`
	APKHashes            map[string]string `json:"apk_hashes"`
	Identity             string            `json:"identity"`
	ProvisionAttempted   bool              `json:"provision_attempted,omitempty"`
	ServerConfigured     bool              `json:"server_configured,omitempty"`
	// 1.3.0 additions. Older snapshots load with zero values and keep their 1.2.0 meaning.
	Tool              string   `json:"tool,omitempty"`
	HomeResolved      string   `json:"home_resolved,omitempty"`
	HomeCandidates    []string `json:"home_candidates,omitempty"`
	Origin            string   `json:"origin,omitempty"`
	Polluted          bool     `json:"polluted,omitempty"`
	Tier              string   `json:"tier,omitempty"`
	Route             string   `json:"route,omitempty"`
	TVCopy            bool     `json:"tv_copy,omitempty"`
	FromTV            bool     `json:"from_tv,omitempty"`
	RebootVerified    bool     `json:"reboot_verified,omitempty"`
	RebootUnreachable bool     `json:"reboot_unreachable,omitempty"`
	ADBCloseRequested bool     `json:"adb_close_requested,omitempty"`
	Warnings          []string `json:"warnings,omitempty"`
	// 1.3.1: what an already-installed client held before this run (nil = not recorded).
	Grants *ClientGrants `json:"client_grants,omitempty"`
	// 1.3.4: the TV's own firmware switches ADB off at every boot.
	ADBOffAtBoot bool `json:"adb_off_at_boot,omitempty"`
	// KeepADBSet: this run told the client to keep ADB on (undone by a restore of an upgrade).
	KeepADBSet bool `json:"keep_adb_set,omitempty"`
	// 1.3.4: the backend entry this TV was connected to, and whether the backend saw it online.
	BackendDevice    string `json:"backend_device,omitempty"`
	BackendRoom      string `json:"backend_room,omitempty"`
	ServerOnline     bool   `json:"server_online,omitempty"`
	BackendNote      string `json:"backend_note,omitempty"`
	BackendKept      bool   `json:"backend_kept,omitempty"`
	ManagedRequested bool   `json:"managed_requested,omitempty"`
	ManagedReady     bool   `json:"managed_ready,omitempty"`
	// Only recorded for the exact firmware whose global idle policy was field verified.
	NativeIdle       map[string]string `json:"native_idle,omitempty"`
	ACPowerRequested bool              `json:"ac_power_requested,omitempty"`
	NativeACPower    *ACPowerSnapshot  `json:"native_ac_power,omitempty"`
}
type InstallRequest struct {
	ACPower         bool   `json:"ac_power"`
	Target          string `json:"target"`
	Server          string `json:"server"`
	Token           string `json:"token"`
	CloseADB        bool   `json:"close_adb"`
	SkipRebootCheck bool   `json:"skip_reboot_check"`
	AllowTrial      bool   `json:"allow_trial"`
	// AllowAvoid is the second, explicit confirmation for models documented as failing elsewhere.
	AllowAvoid bool `json:"allow_avoid"`
	// Register (1.3.4): get the connection from the backend the operator logged into, instead of a
	// server and credential typed by hand. Room is the optional room name for a new entry.
	Register bool   `json:"register,omitempty"`
	Room     string `json:"room,omitempty"`
	Managed  bool   `json:"managed,omitempty"`
}
type RestoreOptions struct {
	RebootCheck bool
	Log         func(string)
	// Exact restores the record as it is (the automatic rollback of a failed run must put back the
	// state right before that run, not jump to first-install and uninstall a working client).
	Exact bool
	// Target overrides the recorded address (DHCP or wireless-debugging port changed).
	Target string
	// Moved reports the address the TV ended on when it differs from the record.
	Moved func(string)
}
type Engine struct {
	Runner      Runner
	Catalog     Catalog
	Root        string
	State       string
	Wait        func(time.Duration)
	CheckServer func(string, string) (ServerCheck, error)
	// Backend is the hotel backend this run registers TVs with (nil = not logged in).
	Backend Registrar
}

func (e Engine) wait(d time.Duration) {
	if e.Wait != nil {
		e.Wait(d)
	} else {
		time.Sleep(d)
	}
}
func (e Engine) detect(raw string) (Facts, Decision, error) {
	target, err := validTarget(raw)
	if err != nil {
		return Facts{}, Decision{}, err
	}
	if err = connect(e.Runner, target); err != nil {
		return Facts{}, Decision{}, err
	}
	f, err := inspect(e.Runner, target)
	return f, decide(e.Catalog, f), err
}
func hasGuard(accessibility string) bool {
	for _, v := range services(accessibility) {
		if v == guard {
			return true
		}
	}
	return false
}
func (e Engine) snapshot(target string, f Facts) (Snapshot, error) {
	paths, err := clientPaths(e.Runner, target)
	if err != nil {
		return Snapshot{}, err
	}
	home, err := readHomeFor(e.Runner, target, f.SDK)
	if err != nil {
		return Snapshot{}, err
	}
	return e.snapshotPaths(target, f, paths, home)
}

// snapshotPaths records everything the install will change, before any change. It only reads.
func (e Engine) snapshotPaths(target string, f Facts, paths []string, home HomeInfo, acPower ...bool) (Snapshot, error) {
	id, err := randomID()
	if err != nil {
		return Snapshot{}, err
	}
	s := Snapshot{ID: id, Target: target, Facts: f, At: time.Now().Format(time.RFC3339), APKHashes: map[string]string{}, Tool: version}
	identity, ie := e.tvIdentity(target)
	if ie != nil {
		return s, errors.New("无法读取电视身份以保护恢复目标，停止安装")
	}
	s.Identity = identity
	if len(acPower) > 0 && acPower[0] {
		s.ACPowerRequested = true
		if err := e.captureACPower(target, &s); err != nil {
			return s, err
		}
	}
	if verifiedNativeIdle(f) {
		s.NativeIdle = map[string]string{}
		for _, key := range nativeIdleKeys {
			value, readErr := shell(e.Runner, target, "settings", "get", key.space, key.name)
			if readErr != nil {
				return s, errors.New("无法备份自动屏保原值，停止安装")
			}
			s.NativeIdle[key.space+"/"+key.name] = strings.TrimSpace(value)
		}
	}
	settings := []struct {
		key string
		dst *string
	}{{"enabled_accessibility_services", &s.Accessibility}, {"accessibility_enabled", &s.AccessibilityEnabled}, {"enabled_notification_listeners", &s.Notification}}
	for _, v := range settings {
		out, err := shell(e.Runner, target, "settings", "get", "secure", v.key)
		if err != nil {
			return s, errors.New("无法备份原有系统设置，停止安装")
		}
		*v.dst = strings.TrimSpace(out)
	}
	if !home.Placeholder {
		s.HomeResolved = home.Resolved
	}
	s.HomeCandidates = home.Candidates
	s.Home = home.originalHome()
	if s.Home == "" {
		return s, errors.New("无法确认原厂桌面：系统只返回选择框或占位界面且没有其他桌面候选，停止安装以保留恢复路径")
	}
	if v, ve := query(e.Runner, target, "getprop", "persist.vt.use_home_pkg"); ve == nil {
		s.VendorHome = strings.TrimSpace(v)
	}
	dir := filepath.Join(e.State, "backups", id)
	if err = os.MkdirAll(dir, 0700); err != nil {
		return s, err
	}
	for _, remote := range paths {
		name := fmt.Sprintf("previous-%d.apk", len(s.APKs))
		path := filepath.Join(dir, name)
		if _, err = e.Runner.Run(target, "pull", remote, path); err != nil {
			return s, errors.New("原 APK 备份失败，未改动电视")
		}
		if st, er := os.Stat(path); er != nil || st.Size() == 0 {
			return s, errors.New("原 APK 备份文件为空")
		}
		s.APKs = append(s.APKs, name)
		b, readErr := os.ReadFile(path)
		if readErr != nil {
			return s, readErr
		}
		s.APKHashes[name] = digest(b)
	}
	s.Installed = len(s.APKs) > 0
	if s.Installed {
		s.Grants = e.readGrants(target, f.SDK)
	}
	// Everything above must describe one TV: the address can change hands while it is read.
	if again, ae := e.tvIdentity(target); ae != nil || again != s.Identity {
		os.RemoveAll(dir)
		return Snapshot{}, errors.New("备份过程中这个地址连到的设备变了（IP 可能被占用），停止安装；未改动电视")
	}
	// First record wins: a retry after an incomplete restore must not replace the TV's real
	// pre-install state with the half-installed one (cleanyourtv recovery.py:12-18).
	s.Origin = e.findOrigin(s.Identity, s.ID)
	s.Polluted = e.polluted(s)
	if err = atomicJSON(filepath.Join(dir, "snapshot.json"), s); err != nil {
		return s, err
	}
	return s, nil
}

// polluted: restoring this snapshot would not bring the TV back to what it was before this
// project touched it (our package already HOME/guarded, or only our current payload installed).
func (e Engine) polluted(s Snapshot) bool {
	if !s.Installed {
		return componentPackage(s.Home) == pkg || hasGuard(s.Accessibility)
	}
	payload := e.payloadHashes()
	if len(payload) == 0 || len(s.APKHashes) == 0 {
		return false
	}
	for _, h := range s.APKHashes {
		if !payload[h] {
			return false
		}
	}
	return true
}
func (e Engine) payloadHashes() map[string]bool {
	out := map[string]bool{}
	for _, route := range []string{"standard", "guard"} {
		if b, err := os.ReadFile(filepath.Join(e.Root, "payload", "hotel-"+route+".apk")); err == nil {
			out[digest(b)] = true
		}
	}
	return out
}
func setting(r Runner, target, key, value string) error {
	if value == "" || value == "null" {
		_, e := shell(r, target, "settings", "delete", "secure", key)
		return e
	}
	_, e := shell(r, target, "settings", "put", "secure", key, value)
	return e
}
func (e Engine) loadSnapshot(id string) (Snapshot, error) {
	var s Snapshot
	if !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(id) {
		return s, errors.New("备份编号无效")
	}
	b, err := os.ReadFile(filepath.Join(e.State, "backups", id, "snapshot.json"))
	if err != nil {
		return s, err
	}
	if err = json.Unmarshal(b, &s); err != nil {
		return s, err
	}
	if s.ID != id {
		return s, errors.New("备份编号不一致")
	}
	return s, nil
}
func (e Engine) saveSnapshot(s Snapshot) error {
	return atomicJSON(filepath.Join(e.State, "backups", s.ID, "snapshot.json"), s)
}

// restoreBaseline decides which recorded state an operator-chosen restore returns the TV to. The
// second result is false when a polluted record had to be repaired by inference (no first record).
func (e Engine) restoreBaseline(sel Snapshot, log func(string)) (Snapshot, bool) {
	if !sel.Polluted {
		return sel, true
	}
	if sel.Origin != "" {
		if o, err := e.loadSnapshot(sel.Origin); err == nil && o.Identity == sel.Identity && !o.Polluted {
			log("所选备份记录的是影院已装上后的状态，改用这台电视首次安装前的备份 " + o.ID)
			o.Target = sel.Target
			if o.NativeACPower == nil && sel.NativeACPower != nil {
				power := *sel.NativeACPower
				o.NativeACPower = &power
				log("首次安装备份未记录通电模式，本项仅按所选备份的已记录原值恢复，不推断出厂值")
			}
			if len(o.NativeIdle) == 0 {
				o.NativeIdle = sel.NativeIdle
			}
			return o, true
		}
	}
	base := sel
	if sel.Installed {
		// Only our current payload was there and no earlier record exists: the meaningful restore
		// is removing it and handing HOME to the vendor launcher.
		log("所选备份里只有本包同版本影院，按“卸载影院、交还原厂桌面”还原")
		base.Installed = false
		base.APKs, base.APKHashes = nil, map[string]string{}
	}
	if componentPackage(base.Home) == pkg {
		base.Home = ""
		for _, c := range sel.HomeCandidates {
			if componentPackage(c) != pkg && !isPlaceholderHome(c) {
				base.Home = c
				break
			}
		}
		base.HomeResolved = base.Home
	}
	if base.VendorHome == pkg {
		base.VendorHome = componentPackage(base.Home)
	}
	base.Accessibility = mergeService(base.Accessibility, guard, false)
	return base, false
}

// expectedFront: where HOME should land on a restored TV — our client when the restored state keeps
// its guard (an older hotel client was installed with it), otherwise the recorded launcher.
func expectedFront(s Snapshot) string {
	if clientTakesOver(s) {
		return pkg
	}
	return componentPackage(s.Home)
}

// clientTakesOver: the restored state has the older client holding HOME — its guard was on, or it
// keeps WRITE_SECURE_SETTINGS, with which it switches the guard back on by itself at every boot.
func clientTakesOver(s Snapshot) bool {
	return s.Installed && (hasGuard(s.Accessibility) || planGrants(s).WSS)
}

func (e Engine) restore(sel Snapshot, opts ...RestoreOptions) error {
	opt := RestoreOptions{}
	if len(opts) > 0 {
		opt = opts[0]
	}
	log := opt.Log
	if log == nil {
		log = func(string) {}
	}
	s, known := sel, true
	if !opt.Exact {
		s, known = e.restoreBaseline(sel, log)
	}
	raw := s.Target
	if opt.Target != "" {
		raw = opt.Target
	}
	target, err := validTarget(raw)
	if err != nil {
		return err
	}
	if err = connect(e.Runner, target); err != nil {
		return err
	}
	f, err := inspect(e.Runner, target)
	if err != nil {
		return err
	}
	if f.Model != s.Facts.Model {
		return errors.New("电视型号与备份不符，拒绝恢复")
	}
	identity, ie := e.tvIdentity(target)
	if ie != nil {
		return errors.New("无法读取电视身份，拒绝恢复（为免改到别的设备）")
	}
	if s.Identity == "" || identity != s.Identity {
		return errors.New("电视身份与备份不符，拒绝跨电视恢复")
	}
	if f.Fingerprint != s.Facts.Fingerprint {
		// The identity check already proves it is the same TV; a firmware update must not lock it
		// out of its own restore (1.2.0 refused here).
		log("提示：电视固件与备份时不同（可能系统升级过）；身份一致，继续还原")
	}
	if !known {
		if o, oe := e.readTVOrigin(target, f, s.Identity); oe == nil && !o.Installed {
			log("改用电视里保存的首次安装前还原点 " + o.ID)
			if o.NativeACPower == nil && s.NativeACPower != nil {
				power := *s.NativeACPower
				o.NativeACPower = &power
				log("电视旧还原点没有通电原值，本项沿用所选备份的已记录原值")
			}
			s = o
		}
	}
	if !opt.Exact && s.NativeACPower != nil {
		power := *s.NativeACPower
		power.Attempted = true
		s.NativeACPower = &power
	}
	if s.Tool == "" && s.HomeResolved == "" {
		s.HomeResolved = s.Home // 1.2.0 records only stored the resolved launcher
	}
	if s.Home == "" || !componentLine.MatchString(s.Home) || isPlaceholderHome(s.Home) {
		return errors.New("备份 HOME 无效")
	}
	for _, name := range s.APKs {
		if !regexp.MustCompile(`^previous-[0-9]+\.apk$`).MatchString(name) {
			return errors.New("备份APK名称无效")
		}
		b, er := os.ReadFile(filepath.Join(e.State, "backups", s.ID, name))
		if er != nil || s.APKHashes[name] == "" || digest(b) != s.APKHashes[name] {
			return errors.New("原APK备份缺失或校验失败，停止恢复")
		}
	}
	if s.Installed {
		err = e.restoreUpgrade(target, f, s, log)
	} else {
		err = e.restoreFresh(target, f, s, log)
	}
	if err != nil {
		return err
	}
	if opt.RebootCheck {
		if target, err = e.verifyRestoreAfterReboot(target, f, s, log); err != nil {
			return err
		}
	}
	if !s.Installed && !opt.Exact {
		query(e.Runner, target, "rm", "-rf", tvRestoreDir)
	}
	if opt.Moved != nil && target != sel.Target {
		opt.Moved(target)
	}
	return nil
}

func (e Engine) handHomeBack(target string, f Facts, home string, failures *[]string, required bool) {
	if f.SDK < cmdPackageSDK {
		return // Android 5–6: the installer never changed the default HOME there
	}
	if f.SDK >= 29 && componentPackage(home) != pkg {
		shell(e.Runner, target, "cmd", "role", "add-role-holder", "--user", "0", "android.app.role.HOME", componentPackage(home))
	}
	if _, er := shell(e.Runner, target, "cmd", "package", "set-home-activity", "--user", "0", home); er != nil {
		if _, er = shell(e.Runner, target, "pm", "set-home-activity", "--user", "0", home); er != nil && required {
			*failures = append(*failures, "默认 HOME 设置恢复失败")
		}
	}
}

// setListener toggles our notification listener. `cmd notification` exists from Android 8; on
// Android 7 it answers "No shell command implementation.", so the setting is merged directly.
func (e Engine) setListener(target string, sdk int, allow bool) error {
	if sdk >= 26 {
		verb := "disallow_listener"
		if allow {
			verb = "allow_listener"
		}
		if _, err := shell(e.Runner, target, "cmd", "notification", verb, listener); err == nil {
			return nil
		}
	}
	cur, err := shell(e.Runner, target, "settings", "get", "secure", "enabled_notification_listeners")
	if err != nil {
		return err
	}
	return setting(e.Runner, target, "enabled_notification_listeners", mergeService(cur, listener, allow))
}

func (e Engine) restoreAccessibility(target string, s Snapshot, keepGuard bool, failures *[]string) {
	now, er := shell(e.Runner, target, "settings", "get", "secure", "enabled_accessibility_services")
	if er != nil {
		*failures = append(*failures, "读取当前无障碍列表失败")
		return
	}
	// Merge only our service membership, preserving services the owner changed after backup.
	restored := mergeService(now, guard, keepGuard)
	if er = setting(e.Runner, target, "enabled_accessibility_services", restored); er != nil {
		*failures = append(*failures, "无障碍绑定恢复失败")
	}
	enabled := s.AccessibilityEnabled
	if restored != strings.Join(services(s.Accessibility), ":") && len(services(restored)) > 0 {
		enabled = "1"
	}
	if er = setting(e.Runner, target, "accessibility_enabled", enabled); er != nil {
		*failures = append(*failures, "无障碍总开关恢复失败")
	}
}

func (e Engine) restoreVendorHome(target string, s Snapshot, failures *[]string) {
	if s.VendorHome != "" {
		if _, er := shell(e.Runner, target, "setprop", "persist.vt.use_home_pkg", s.VendorHome); er != nil {
			*failures = append(*failures, "原厂 HOME 属性恢复失败")
		}
	}
}

// settleHome waits until the TV shows what it showed before installation. A chooser counts only
// when the TV had no explicit default launcher before; FallbackHome and setup screens never count.
func (e Engine) settleHome(target string, s Snapshot, attempts int, every time.Duration) (string, error) {
	got, err := e.waitForeground(target, expectedFront(s), false, attempts, 2, every)
	if err != nil && s.HomeResolved == "" && got == "android" {
		return e.waitForeground(target, "android", false, 4, 2, every)
	}
	return got, err
}

// confirmHome proves where HOME goes now: starting a launcher by name proves nothing about which
// app is the default, so the decisive check is a HOME key press.
func (e Engine) confirmHome(target string, s Snapshot, failures *[]string) {
	shell(e.Runner, target, "am", "start", "-W", "-n", s.Home)
	if _, er := shell(e.Runner, target, "input", "keyevent", "KEYCODE_HOME"); er != nil {
		*failures = append(*failures, "无法发送主页键")
		return
	}
	if got, err := e.settleHome(target, s, 10, 1500*time.Millisecond); err != nil {
		*failures = append(*failures, "按主页键没有回到原桌面（当前前台："+got+"）")
	}
}

// restoreFresh returns a TV that had no hotel client back to its pre-install state: the guard and
// every way the app revives itself are disabled first (BootReceiver + WRITE_SECURE_SETTINGS would
// otherwise re-add the guard on the next boot — the 1.2.0 restore looked done for one second and
// came back after a reboot), HOME goes back to the vendor launcher, then the app is uninstalled.
func (e Engine) restoreFresh(target string, f Facts, s Snapshot, log func(string)) error {
	var failures []string
	paths, perr := clientPaths(e.Runner, target)
	present := perr != nil || len(paths) > 0
	log("停用影院守护与开机自恢复")
	if present {
		shell(e.Runner, target, "am", "broadcast", "-a", pkg+".CONFIG", "-n", pkg+"/.guard.ConfigReceiver", "--ez", "keep_adb", "false")
		shell(e.Runner, target, "pm", "revoke", pkg, "android.permission.WRITE_SECURE_SETTINGS")
	}
	current, readErr := shell(e.Runner, target, "settings", "get", "secure", "enabled_accessibility_services")
	if readErr != nil {
		return readErr
	}
	if err := setting(e.Runner, target, "enabled_accessibility_services", mergeService(current, guard, false)); err != nil {
		failures = append(failures, "停用本应用守护失败")
	}
	if present {
		e.setListener(target, f.SDK, false)
		shell(e.Runner, target, "appops", "set", pkg, "SYSTEM_ALERT_WINDOW", "default")
		shell(e.Runner, target, "am", "force-stop", pkg)
	}
	e.restoreVendorHome(target, s, &failures)
	log("把默认桌面交还 " + s.Home)
	e.handHomeBack(target, f, s.Home, &failures, !present)
	if present {
		log("卸载影院客户端，回到首次安装前的状态")
		out, er := shell(e.Runner, target, "pm", "uninstall", pkg)
		if er != nil || !strings.Contains(out, "Success") {
			failures = append(failures, "影院客户端卸载失败（已尝试收回守护权限；它开机时仍可能自行打开，需在电视设置里人工卸载）")
		} else if after, ae := clientPaths(e.Runner, target); ae != nil || len(after) > 0 {
			failures = append(failures, "卸载后仍检测到影院客户端")
		}
	}
	e.restoreAccessibility(target, s, false, &failures)
	e.restoreNativeIdle(target, s, &failures)
	e.restoreACPower(target, f, s, &failures)
	e.confirmHome(target, s, &failures)
	if len(failures) > 0 {
		return errors.New(strings.Join(failures, "；"))
	}
	return nil
}

// restoreUpgrade puts the previously installed hotel client back (same rules as 1.2.0): private
// connection/PIN overrides are rolled back first, otherwise the current APK and its rollback data stay.
func (e Engine) restoreUpgrade(target string, f Facts, s Snapshot, log func(string)) error {
	var failures []string
	if s.KeepADBSet {
		// This run told the client to keep ADB on; the older client did not.
		shell(e.Runner, target, "am", "broadcast", "-a", pkg+".CONFIG", "-n", pkg+"/.guard.ConfigReceiver", "--ez", "keep_adb", "false")
	}
	if !planGrants(s).WSS {
		// Before the guard is removed: while the client holds this permission it puts the guard
		// straight back (ConfigReceiver and every revival path call GuardSetup.ensureEnabled).
		shell(e.Runner, target, "pm", "revoke", pkg, wssPermission)
	}
	current, readErr := shell(e.Runner, target, "settings", "get", "secure", "enabled_accessibility_services")
	if readErr != nil {
		return readErr
	}
	if err := setting(e.Runner, target, "enabled_accessibility_services", mergeService(current, guard, false)); err != nil {
		failures = append(failures, "临时停用本应用守护失败")
	}
	rollback, rollbackErr := shell(e.Runner, target, "am", "broadcast", "-a", pkg+".CONFIG", "-n", pkg+"/.guard.ConfigReceiver", "--es", "provision_rollback", s.ID)
	configRestored := !s.ProvisionAttempted || (rollbackErr == nil && regexp.MustCompile(`result=200\b`).MatchString(rollback) && strings.Contains(rollback, "hotel_provisioning_restored"))
	if !configRestored {
		failures = append(failures, "本次连接/PIN配置回退未确认；保留当前APK及应用私有恢复资料，请人工处理")
	}
	shell(e.Runner, target, "am", "force-stop", pkg)
	if configRestored {
		log("回装原客户端 APK（不卸载、不清数据）")
		sub, args := "install-multiple", []string{"-r", "-d"}
		if len(s.APKs) == 1 {
			sub = "install"
		}
		for _, name := range s.APKs {
			args = append(args, filepath.Join(e.State, "backups", s.ID, name))
		}
		if out, er := adbInstallCmd(e.Runner, target, sub, args...); er != nil {
			_, advice := installAdvice(out)
			failures = append(failures, "原客户端 APK 恢复失败（没有卸载或清数据）："+advice+"（电视返回："+tvReply(out)+"）")
		}
	}
	log("把旧客户端的权限恢复为安装前状态")
	e.restoreClientGrants(target, f, s, log, &failures)
	e.restoreAccessibility(target, s, clientTakesOver(s), &failures)
	if err := e.setListener(target, f.SDK, hasListener(s.Notification)); err != nil {
		failures = append(failures, "通知绑定需人工核对")
	}
	e.restoreVendorHome(target, s, &failures)
	e.handHomeBack(target, f, s.Home, &failures, true)
	e.restoreNativeIdle(target, s, &failures)
	e.restoreACPower(target, f, s, &failures)
	e.confirmHome(target, s, &failures)
	if len(failures) > 0 {
		return errors.New(strings.Join(failures, "；"))
	}
	return nil
}

func hasListener(raw string) bool {
	for _, v := range services(raw) {
		if v == listener {
			return true
		}
	}
	return false
}

// verifyInstalled reads back what the TV actually installed and compares it with the payload.
func (e Engine) verifyInstalled(target, apk string) error {
	want, err := os.ReadFile(apk)
	if err != nil {
		return errors.New("随包APK无法读取")
	}
	got, err := e.installedDigest(target)
	if err != nil {
		return err
	}
	if got != digest(want) {
		return errors.New("电视上实际安装的APK与随包客户端不一致，停止并恢复")
	}
	return nil
}

// installedDigest reads back the client's installed base APK and returns its SHA256.
func (e Engine) installedDigest(target string) (string, error) {
	paths, err := clientPaths(e.Runner, target)
	if err != nil || len(paths) == 0 {
		return "", errors.New("安装后电视没有登记影院客户端")
	}
	remote := paths[0]
	for _, p := range paths {
		if strings.HasSuffix(p, "/base.apk") {
			remote = p
		}
	}
	tmp := filepath.Join(e.State, "tmp")
	if err = os.MkdirAll(tmp, 0700); err != nil {
		return "", err
	}
	local := filepath.Join(tmp, "installed-check.apk")
	defer os.Remove(local)
	if _, err = e.Runner.Run(target, "pull", remote, local); err != nil {
		return "", errors.New("无法读回电视上已安装的APK")
	}
	got, err := os.ReadFile(local)
	if err != nil {
		return "", errors.New("无法读回电视上已安装的APK")
	}
	return digest(got), nil
}

func (e Engine) install(req InstallRequest, pinHash string, log func(string)) (snap Snapshot, err error) {
	if req.Managed && (!req.Register || req.CloseADB || req.SkipRebootCheck || strings.TrimSpace(req.Room) == "") {
		return snap, errors.New("后台纳管须逐台接入后台、填写房号、完成重启验证并保持调试；不能同时选择关闭调试")
	}
	req.Server = normalizeServer(req.Server)
	req.Token = strings.TrimSpace(req.Token)
	target, err := validTarget(req.Target)
	if err != nil {
		return snap, err
	}
	if err = connectionProblem(req.Server, req.Token); err != nil {
		return snap, err
	}
	if h := adminPortHint(req.Server); h != "" {
		log("提示：" + h)
	}
	req.Room = strings.TrimSpace(req.Room)
	if req.Register {
		if req.Server != "" || req.Token != "" {
			return snap, errors.New("登录后台自动登记时不用填写服务器地址和设备凭证")
		}
		if err = roomProblem(req.Room); err != nil {
			return snap, err
		}
		if e.Backend == nil {
			return snap, errors.New("还没有登录后台：请在“本店配置”填写服务器 IP 和管理员账号后点“登录后台”")
		}
		// Before the TV is touched: a session that ended would only show up after the install.
		if err = e.Backend.Ready(); err != nil {
			return snap, err
		}
	}
	if pinHash != "" && !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(pinHash) {
		return snap, errors.New("本店 PIN 配置无效")
	}
	log("连接并重新检测电视")
	f, d, err := e.detect(target)
	if err != nil {
		return snap, err
	}
	if !d.Allowed && !d.Trial {
		return snap, errors.New(d.Reason)
	}
	if !d.Allowed && !req.AllowTrial {
		return snap, errors.New("该机型未经本项目实测：需勾选“允许试装”才会安装（试装会做重启验证，失败自动还原）。" + d.Reason)
	}
	if d.Avoid && !req.AllowAvoid {
		return snap, errors.New("网上记录这款电视接管失败（不建议）：需要再次确认才会试装；未改动电视。" + d.Reason)
	}
	// Only this project's own verified firmwares may skip the cold-boot check; everything proven
	// elsewhere or on trial also gets the two-minute stability watch.
	rebootCheck := !req.SkipRebootCheck || d.Tier != "verified"
	observe := d.Tier != "verified"
	for _, w := range d.Warnings {
		log("提示：" + w)
	}
	paths, err := clientPaths(e.Runner, target)
	if err != nil {
		return snap, err
	}
	if len(paths) == 0 && pinHash == "" {
		return snap, errors.New("检测到首次安装：请生成或保存本店员工PIN即可安装；服务器可稍后接入，尚未改动电视")
	}
	// The TV's own identity, read before anything happens: every later step (reboots, backup,
	// changes, restore) must reach this very TV, not whatever answers at the address afterwards.
	identity, err := e.tvIdentity(target)
	if err != nil {
		return snap, errors.New("无法读取电视身份以保护恢复目标，停止安装；未改动电视")
	}
	log("只读预检：默认桌面、前台界面与系统设置是否可读")
	home, err := readHomeFor(e.Runner, target, f.SDK)
	if err != nil {
		return snap, errors.New(err.Error() + "；未改动电视")
	}
	if fg, src, fe := foreground(e.Runner, target); fe != nil {
		return snap, errors.New("无法读取电视前台界面，本工具无法验收 HOME；未改动电视。请点“只读体检”保存报告")
	} else {
		log("前台读取正常（" + src + "：" + fg + "）")
	}
	log("保存原 HOME、权限绑定和已安装 APK")
	snap, err = e.snapshotPaths(target, f, paths, home, req.ACPower)
	if err != nil {
		// A generated ID is not a restore point until snapshot.json was successfully saved.
		if snap.ID != "" {
			if _, readErr := e.loadSnapshot(snap.ID); readErr != nil {
				snap.ID = ""
			}
		}
		return snap, err
	}
	discard := func(msg string) (Snapshot, error) {
		// Nothing was changed: a backup of this attempt would only clutter the restore list.
		os.RemoveAll(filepath.Join(e.State, "backups", snap.ID))
		return Snapshot{}, errors.New(msg)
	}
	if snap.Identity != identity {
		return discard("备份时这个地址连到的已不是检测的那台电视（IP 可能被占用），停止；未改动电视")
	}
	snap.Tier, snap.Route, snap.Warnings = d.Tier, d.Route, d.Warnings
	e.saveSnapshot(snap)
	if err = e.upgradeAllowed(snap); err != nil {
		return snap, err
	}
	legacy := e.legacyDefaults(snap)
	if strings.Contains(snap.Accessibility, "app.orchard.tv/") {
		return snap, errors.New("检测到旧 Aerial 守护，请先用其原安装工具恢复；本工具不会删除其他无障碍服务")
	}
	// keepADB: this TV switches ADB off at every boot; the hotel client keeps it on during the
	// verification instead (see adbkeep.go).
	keepADB := req.Managed
	if e.adbOffKnown(identity) || e.adbOffSeenThisBoot(target) {
		keepADB = true
		e.rememberAdbOff(identity, f)
		log("这台电视开机时系统会自动关闭 ADB 调试（小米等常见）：跳过预检重启；装好后由影院客户端在验证期间保持调试开着，重启验证和失败还原照常进行")
	} else {
		// The automatic restore depends on ADB surviving a reboot (`adb tcpip` and some wireless-
		// debugging setups do not). That is a setting of each TV, not of its firmware, so every TV is
		// checked — this project's own verified firmwares too, since another TV of the same model may
		// have debugging switched on only temporarily (Codex reviews of 1.3.0 and 1.3.1). It runs
		// after the read-only refusals above, so no TV is rebooted only to be turned down, and before
		// any change.
		log("预检重启：先确认这台电视重启后调试连接还能恢复（失败时才能自动还原），尚未改动电视")
		newTarget, rerr := e.rebootAndReconnect(target, identity, log)
		switch {
		case errors.Is(rerr, errADBOffAfterBoot):
			e.rememberAdbOff(identity, f)
			return discard("这台电视开机后，系统会自动关闭 ADB 调试（已记下这台电视）。请在电视设置里重新打开 ADB 调试，再点一次“一键安装”：下次会跳过预检重启，装好后由影院客户端在验证期间保持调试开着。没有安装、也没有改动电视")
		case errors.Is(rerr, errWrongTV):
			return discard("预检重启前后核对身份时，这个地址连到的不是原来那台电视（IP 可能被另一台设备占用）。没有安装、也没有改动任何电视；请在电视上核对 IP 后重新检测")
		case errors.Is(rerr, errIdentityUnreadable):
			return discard("预检重启前后无法确认电视身份（身份读取失败），为免误操作别的设备已停止；没有安装、也没有改动电视，请重试")
		case rerr != nil:
			return discard("电视重启后调试连接没有恢复（常见于临时开启的网络调试或无线调试）。这样安装失败时无法自动还原，所以没有安装、也没有改动电视。请在电视设置里开启开机后仍保持的网络调试再试")
		}
		if newTarget != target {
			log("电视重启后调试端口变为 " + newTarget)
			target, snap.Target = newTarget, newTarget
			e.saveSnapshot(snap)
		}
		// The channel survived, but perhaps only because an installed client already keeps ADB on:
		// this boot's log still shows the firmware switching it off.
		if e.adbOffSeenThisBoot(target) {
			keepADB = true
			e.rememberAdbOff(identity, f)
			log("预检重启时电视系统关闭过 ADB 调试（已由已装的影院客户端重新打开）：装好后同样由影院客户端在验证期间保持调试")
		}
	}
	if keepADB {
		snap.ADBOffAtBoot = true
		e.saveSnapshot(snap)
	}
	// Last check before the first change on the TV (the restore point, then the APK).
	if err = e.sameTV(target, identity); err != nil {
		return snap, errors.New("改动电视前核对身份未通过（" + err.Error() + "），停止；未改动电视")
	}
	if snap.TVCopy = e.pushRestorePoint(target, snap); snap.TVCopy {
		log("还原点已同时写入电视（换电脑也能还原）")
		e.saveSnapshot(snap)
	}
	mutated := false
	var reg *registration
	defer func() {
		e.recordField(f, snap, err)
		if err != nil && mutated {
			log("安装未通过，正在恢复原 HOME 和已备份 APK")
			re := e.restore(snap, RestoreOptions{Log: log, RebootCheck: rebootCheck, Exact: true})
			if errors.Is(re, errRestoreNotRechecked) {
				err = fmt.Errorf("%v；已执行恢复（%v），备份 %s", err, re, snap.ID)
			} else if re != nil {
				err = fmt.Errorf("%v；自动恢复未完整通过：%v。保留备份 %s", err, re, snap.ID)
			} else {
				err = fmt.Errorf("%v；已执行恢复，备份 %s", err, snap.ID)
			}
			// The TV no longer uses the backend entry this run created; a retry starts clean.
			if reg != nil && reg.Created {
				if re == nil || errors.Is(re, errRestoreNotRechecked) {
					if ae := e.Backend.Discard(reg.ID); ae == nil {
						log("这次在后台新建的设备 " + reg.Room + " 已删除（电视已还原，没有用上它）")
					} else {
						log("提示：这次在后台新建的设备 " + reg.Room + "（" + reg.ID + "）未能删除：" + ae.Error() + "；可在后台“客房设备”处理")
					}
				} else {
					log("提示：后台新建的设备 " + reg.Room + "（" + reg.ID + "）保留：电视的还原未完整通过，确认电视状态后可在后台删除")
				}
			}
		}
	}()
	route := d.Route
	if f.SDK < cmdPackageSDK && snap.Installed {
		for _, c := range home.Candidates {
			if componentPackage(c) == pkg {
				// Android 5–6 upgrade where the old hotel client is the default HOME: keep a
				// HOME-declaring build, because the default could not be set back there.
				route = "standard"
			}
		}
	}
	apk := filepath.Join(e.Root, "payload", "hotel-"+route+".apk")
	if err = e.sameTV(target, identity); err != nil {
		return snap, errors.New("安装前核对身份未通过（" + err.Error() + "），停止；只写入了还原点文件")
	}
	log("安装同签名客户端（保留原数据）")
	out, er := adbInstall(e.Runner, target, "-r", apk)
	if er != nil {
		code, advice := installAdvice(out)
		log("电视返回：" + tvReply(out))
		if code == "" {
			// No failure code: the reply may have been lost after the package was committed (the
			// connection cut by the firmware). Look at what the TV really has before saying anything.
			switch e.installedAfterLostReply(target, apk, snap) {
			case "present":
				mutated = true
				return snap, errors.New("安装回执丢失（调试连接被中断），但电视上已经装上了本包客户端；按失败处理")
			case "unknown":
				mutated = true
				return snap, errors.New("安装回执丢失，且调试连接已断开，无法确认电视上是否已装上客户端；请在电视设置重新打开 ADB 调试后用备份还原 " + snap.ID)
			}
		}
		if !strings.HasPrefix(code, "INSTALL_FAILED_VERIFICATION") || route != "standard" {
			return snap, errors.New(advice + "（电视返回：" + tvReply(out) + "）")
		}
		log("电视安装校验拦截了 standard 版，改用不声明桌面的 guard 版重试（同 Aerial 做法）")
		route = "guard"
		apk = filepath.Join(e.Root, "payload", "hotel-guard.apk")
		if out, er = adbInstall(e.Runner, target, "-r", apk); er != nil {
			_, advice = installAdvice(out)
			log("电视返回：" + tvReply(out))
			return snap, errors.New(advice + "（电视返回：" + tvReply(out) + "）")
		}
	}
	mutated = true
	snap.Route = route
	e.saveSnapshot(snap)
	if err = e.applyNativeIdle(target, snap, log); err != nil {
		return snap, err
	}
	if err = e.applyACPower(target, &snap, log); err != nil {
		return snap, err
	}
	log("核对电视上实际安装的 APK")
	if err = e.verifyInstalled(target, apk); err != nil {
		return snap, err
	}
	critical := [][]string{{"pm", "grant", pkg, "android.permission.WRITE_SECURE_SETTINGS"}}
	optional := [][]string{{"appops", "set", pkg, "GET_USAGE_STATS", "allow"}, {"appops", "set", pkg, "android:get_usage_stats", "allow"}, {"appops", "set", pkg, "APP_AUTO_START", "allow"}, {"appops", "set", pkg, "APP_ASSOC_START", "allow"}, {"appops", "set", pkg, "AUTO_START", "allow"}, {"appops", "set", pkg, "ASSOC_START", "allow"}}
	// Starting the app from BootReceiver needs the overlay exemption only from Android 10.
	saw := []string{"appops", "set", pkg, "SYSTEM_ALERT_WINDOW", "allow"}
	if f.SDK >= 29 {
		critical = append(critical, saw)
	} else {
		optional = append(optional, saw)
	}
	if f.SDK >= 33 {
		// Android 13+ can switch accessibility off for apps it regards as sideloaded (Aerial #811 #829).
		optional = append(optional, []string{"appops", "set", pkg, "ACCESS_RESTRICTED_SETTINGS", "allow"})
	}
	critical = append(critical, []string{"settings", "put", "secure", "enabled_accessibility_services", mergeService(snap.Accessibility, guard, true)}, []string{"settings", "put", "secure", "accessibility_enabled", "1"})
	for _, cmd := range critical {
		if _, er = shell(e.Runner, target, cmd...); er != nil {
			return snap, errors.New("电视拒绝必要的守护权限")
		}
	}
	bound, be := shell(e.Runner, target, "settings", "get", "secure", "enabled_accessibility_services")
	enabled, ee := shell(e.Runner, target, "settings", "get", "secure", "accessibility_enabled")
	if be != nil || ee != nil || !hasGuard(bound) || strings.TrimSpace(enabled) != "1" {
		return snap, errors.New("守护绑定写入后读回未确认，停止安装并恢复；不能只按ADB退出码判断权限已生效")
	}
	for _, cmd := range optional {
		if _, er = shell(e.Runner, target, cmd...); er != nil {
			log("一个厂商可选权限不适用；继续以 HOME 实际结果判断")
		}
	}
	if er = e.setListener(target, f.SDK, true); er != nil {
		log("通知监听未能授权；守护复活改靠同步适配器，继续以 HOME 实际结果判断")
	}
	if err = e.ensureGuardBound(target, bound, log); err != nil {
		return snap, err
	}
	if route == "standard" {
		if f.SDK >= 29 {
			if _, er = shell(e.Runner, target, "cmd", "role", "add-role-holder", "--user", "0", "android.app.role.HOME", pkg); er != nil {
				log("系统 HOME 角色未接受，改用默认桌面设置并以实际 HOME 检查为准")
			}
		}
		if _, er = shell(e.Runner, target, "cmd", "package", "set-home-activity", "--user", "0", activity); er != nil {
			shell(e.Runner, target, "pm", "set-home-activity", "--user", "0", activity)
		}
		if snap.VendorHome != "" {
			if _, er = shell(e.Runner, target, "setprop", "persist.vt.use_home_pkg", pkg); er != nil {
				log("厂商 HOME 属性未允许写入，等待实际 HOME 检查")
			}
		}
		if h, he := readHome(e.Runner, target); he == nil {
			if componentPackage(h.Resolved) == pkg {
				log("系统默认桌面已指向影院")
			} else {
				log("系统默认桌面仍为 " + h.Resolved + "，由守护接管 HOME 键")
			}
		}
	}
	out, er = shell(e.Runner, target, "am", "start", "-W", "-n", activity)
	if er != nil || !strings.Contains(out, "Status: ok") {
		return snap, errors.New("客户端启动未通过")
	}
	// keep_adb is the client's own preference (an earlier run may have set it on request); it is only
	// changed where this run needs it: on here, and off below when debugging is to be closed.
	if keepADB {
		e.setKeepADB(target, true)
		snap.KeepADBSet = true
		e.saveSnapshot(snap)
		log("验证期间由影院客户端保持 ADB 调试开着（电视系统开机会关闭它）")
	}
	log("检查守护与 HOME 返回（两轮，回到影院后须稳定停留）")
	e.wait(3 * time.Second)
	if err = e.homeKeyReturns(target, 2); err != nil {
		log(err.Error())
		return snap, err
	}
	if req.Register {
		var note string
		reg, req.Server, req.Token, note = e.registerTV(target, snap, f, req.Room, log)
		snap.BackendNote = note
		if reg != nil {
			snap.BackendDevice, snap.BackendRoom, snap.BackendKept = reg.ID, reg.Room, reg.Kept
		}
		e.saveSnapshot(snap)
	}
	if req.Server != "" {
		log("保存连接信息及PIN，不要求服务器此时在线")
	} else {
		log("保留已有连接或稍后接入服务器，写入本店PIN")
	}
	if req.Server != "" || pinHash != "" || legacy != "" {
		snap.ProvisionAttempted = true
		if er = e.saveSnapshot(snap); er != nil {
			return snap, errors.New("无法保存配置恢复标记，停止写入")
		}
		args := []string{"am", "broadcast", "-a", pkg + ".CONFIG", "-n", pkg + "/.guard.ConfigReceiver"}
		if req.Server != "" {
			args = append(args, "--es", "provision_server", strings.TrimRight(req.Server, "/"), "--es", "provision_token", req.Token)
		}
		if pinHash != "" {
			args = append(args, "--es", "staff_pin_sha256", pinHash)
		}
		args = append(args, "--es", "provision_id", snap.ID)
		if legacy != "" {
			args = append(args, "--es", "legacy_defaults", legacy)
		}
		out, er = shell(e.Runner, target, args...)
		if er != nil || !regexp.MustCompile(`result=200\b`).MatchString(out) || !strings.Contains(out, "hotel_provisioned") {
			return snap, errors.New("配置接口未确认成功；不把旧客户端当作配置完成")
		}
		shell(e.Runner, target, "am", "force-stop", pkg)
		out, er = shell(e.Runner, target, "am", "start", "-W", "-n", activity)
		if er != nil || !strings.Contains(out, "Status: ok") {
			return snap, errors.New("配置后重新启动未通过")
		}
		// Android removes a force-stopped app's accessibility services from the enabled list (the
		// same mechanism as Xiaomi's standby cleanup), so the guard and HOME are checked again.
		if err = e.recheckGuard(target, log); err != nil {
			return snap, err
		}
	}
	status, se := shell(e.Runner, target, "am", "broadcast", "-a", pkg+".CONFIG", "-n", pkg+"/.guard.ConfigReceiver", "--ez", "provision_status", "true")
	configured, pinReady, statusErr := provisioningState(status)
	if se != nil || statusErr != nil {
		return snap, errors.New("电视配置状态回执无效，不能确认安装完成")
	}
	if req.Server != "" && !configured {
		return snap, errors.New("电视未确认服务器连接信息已保存，不能验收为成功")
	}
	if !pinReady {
		return snap, errors.New("电视未确认员工PIN已配置，不能验收为成功")
	}
	snap.ServerConfigured = configured
	if reg != nil && configured {
		snap.ServerOnline = e.confirmOnline(reg, log)
		snap.BackendRoom = reg.Room
		e.saveSnapshot(snap)
	}
	shell(e.Runner, target, "cmd", "package", "compile", "-m", "speed", "-f", pkg)
	if rebootCheck {
		log("等待系统写盘后软件重启电视做重启验证（adb reboot，约 1–3 分钟；通过后才关闭调试。拔电开机不在此列）")
		newTarget, rerr := e.rebootAndReconnect(target, snap.Identity, log)
		switch {
		case errors.Is(rerr, errWrongTV):
			// The automatic restore that follows checks identity first and never touches that device.
			return snap, errors.New("重启验证时这个地址连到的不是这台电视（IP 可能被另一台设备占用），无法验收；请到电视前确认开机画面")
		case errors.Is(rerr, errIdentityUnreadable):
			return snap, errors.New("重启验证前后无法确认电视身份（身份读取失败），为免误操作别的设备不能验收")
		case rerr != nil:
			// No channel to restore through: keep everything, say so plainly, never claim success.
			snap.RebootUnreachable = true
			e.saveSnapshot(snap)
			if keepADB && errors.Is(rerr, errADBOffAfterBoot) {
				log("重启后电视系统关闭了调试，影院客户端没能把它重新打开：请到电视前确认开机画面；需要还原时在电视设置里打开 ADB 调试，再用备份 " + snap.ID + " 还原")
			} else {
				log("重启后无法重新连接电视调试：请到电视前确认开机画面；未关闭调试，备份 " + snap.ID + " 保留")
			}
			return snap, nil
		}
		if newTarget != target {
			log("电视重启后调试端口变为 " + newTarget)
			target = newTarget
			snap.Target = newTarget
		}
		if err = e.verifyBoot(target, snap.Identity, log); err != nil {
			return snap, err
		}
		if observe {
			if err = e.observeStability(target, log); err != nil {
				return snap, err
			}
		}
		snap.RebootVerified = true
	}
	if keepADB && !req.CloseADB {
		log("验证完成：按你的选择（不关闭调试），影院客户端以后每次开机都会把 ADB 调试重新打开")
	}
	if req.Managed {
		snap.ManagedRequested = true
		if !snap.ServerOnline || !snap.RebootVerified || snap.BackendDevice == "" {
			snap.BackendNote = "首装已完成，但真实上线或重启未通过，不能标记后台纳管完成"
		} else if registrar, ok := e.Backend.(fleetRegistrar); ok {
			if err := registrar.EnrollManaged(snap.BackendDevice, target, snap.Identity, log); err != nil {
				snap.BackendNote = err.Error()
				log(snap.BackendNote)
			} else {
				snap.ManagedReady = true
				log("后台纳管已核验：设备身份、服务器维护授权、房号、真实上线及重启检查通过")
			}
		} else {
			snap.BackendNote = "此后台连接不支持纳管交接，未标记纳管成功"
		}
	}
	if configured {
		log("本地安装与配置自动检查通过；服务器在线及播放仍待验收")
	} else {
		log("电视端安装与PIN自动检查通过；服务器可稍后在员工维护页配置")
	}
	// Commit installation before an intentional ADB disconnect. A transport disconnect is not
	// accepted as proof of shutdown: TCP is rechecked separately and the operator must confirm
	// the system setting on the TV. Network failure alone is not proof the setting is off.
	if req.CloseADB && (snap.RebootVerified || !rebootCheck) {
		// A client keeping ADB (this run or an earlier one) would switch it straight back on.
		e.setKeepADB(target, false)
		if keepADB {
			log("验证完成：影院客户端不再保持调试（电视下次开机会照常关闭 ADB）")
		}
		shell(e.Runner, target, "settings", "put", "global", "adb_enabled", "0")
		snap.ADBCloseRequested = true
		log("已请求关闭调试；可在电视维护菜单重新开启。端口关闭情况见任务结果")
	}
	e.saveSnapshot(snap)
	return snap, nil
}

// ensureGuardBound waits for the system to bind the guard, re-adding it once when it stays unbound
// (Aerial GuardSetup.kt:309-342). Firmware without a readable services dump falls back to the HOME test.
func (e Engine) ensureGuardBound(target, bound string, log func(string)) error {
	state := "unknown"
	for attempt := 0; attempt < 2; attempt++ {
		for i := 0; i < 5; i++ {
			e.wait(2 * time.Second)
			if state = guardBinding(e.Runner, target); state != "unbound" {
				break
			}
		}
		if state != "unbound" || attempt == 1 {
			break
		}
		setting(e.Runner, target, "enabled_accessibility_services", mergeService(bound, guard, false))
		e.wait(1500 * time.Millisecond)
		setting(e.Runner, target, "enabled_accessibility_services", mergeService(bound, guard, true))
	}
	switch state {
	case "bound":
		log("系统已实际绑定影院守护")
	case "unbound":
		return errors.New("系统没有绑定影院守护（无障碍服务写入后未生效），停止安装并恢复")
	default:
		log("本机无法读取服务绑定状态，以实际 HOME 检查为准")
	}
	return nil
}

func (e Engine) homeKeyReturns(target string, rounds int) error {
	for i := 0; i < rounds; i++ {
		if _, er := shell(e.Runner, target, "input", "keyevent", "KEYCODE_HOME"); er != nil {
			return er
		}
		if got, err := e.waitForeground(target, pkg, false, 12, 3, 2*time.Second); err != nil {
			message := "按 HOME 未稳定回到影院（最后前台：" + got + "），不能验收为成功"
			if status := guardDiagnostic(e.Runner, target); status != "" {
				message += "；守护诊断：" + status
			}
			return errors.New(message)
		}
	}
	return nil
}

func (e Engine) recheckGuard(target string, log func(string)) error {
	cur, err := shell(e.Runner, target, "settings", "get", "secure", "enabled_accessibility_services")
	if err != nil {
		return errors.New("配置后无法读取守护状态")
	}
	if !hasGuard(cur) {
		log("客户端重启后系统移除了守护，重新写入")
		if err = setting(e.Runner, target, "enabled_accessibility_services", mergeService(cur, guard, true)); err != nil {
			return errors.New("配置后无法重新写入守护")
		}
		setting(e.Runner, target, "accessibility_enabled", "1")
	}
	if err = e.ensureGuardBound(target, cur, log); err != nil {
		return err
	}
	return e.homeKeyReturns(target, 1)
}

func provisioningState(status string) (bool, bool, error) {
	if !regexp.MustCompile(`result=200\b`).MatchString(status) {
		return false, false, errors.New("configuration status rejected")
	}
	hotel := regexp.MustCompile(`\bhotel_configured=(true|false)\b`).FindStringSubmatch(status)
	pin := regexp.MustCompile(`\bpin_configured=(true|false)\b`).FindStringSubmatch(status)
	if hotel == nil || pin == nil {
		return false, false, errors.New("incomplete configuration status")
	}
	return hotel[1] == "true", pin[1] == "true", nil
}
func digest(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }

// tvReply is the tail of what adb printed, for the log (paths on this PC stay; nothing secret is
// sent to an install command).
func tvReply(out string) string {
	t := strings.TrimSpace(lastLines(out, 3))
	if t == "" {
		return "（没有任何输出）"
	}
	if r := []rune(t); len(r) > 300 {
		t = string(r[len(r)-300:])
	}
	return strings.ReplaceAll(t, "\n", " / ")
}

// installedAfterLostReply tells whether the payload ended up installed although adb reported no
// result: "present", "absent" or "unknown" (the TV cannot be asked).
func (e Engine) installedAfterLostReply(target, apk string, s Snapshot) string {
	e.Runner.Run("", "connect", target)
	paths, err := clientPaths(e.Runner, target)
	if err != nil {
		return "unknown"
	}
	if len(paths) == 0 {
		return "absent"
	}
	if !s.Installed {
		return "present"
	}
	// Upgrade: "absent" only when the old client is provably still there, byte for byte; a read
	// that fails (the link dropped again) proves nothing.
	got, err := e.installedDigest(target)
	if err != nil {
		return "unknown"
	}
	if want, rerr := os.ReadFile(apk); rerr == nil && got == digest(want) {
		return "present"
	}
	for _, h := range s.APKHashes {
		if h == got {
			return "absent"
		}
	}
	return "unknown"
}
