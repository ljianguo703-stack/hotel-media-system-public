package main

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type fakeADB struct {
	calls           []string
	model           string
	brand           string
	device          string
	fingerprint     string
	identity        string
	accessible      string
	home            string
	focus           string
	installed       bool
	failHome        bool
	rejectInstall   bool
	rejectGrant     bool
	rejectProvision bool
	missingConfig   bool
	previousAPK     []byte
	installedAPK    []byte
	configured      bool
	pinConfigured   bool
	bootID          int
	tvFiles         map[string]bool
	nativeIdle      map[string]string
}

func fake() *fakeADB {
	return &fakeADB{model: "TestTV", identity: "test-identity-A", accessible: "com.voice/.Reader", home: "com.vendor/.Home", focus: "com.vendor/.Home", tvFiles: map[string]bool{}}
}

// shellInner strips the exit-status marker the engine appends to every shell command.
func shellInner(cmd string) (string, bool) {
	suffix := "; echo " + rcMarker + "$?"
	if strings.HasSuffix(cmd, suffix) {
		return strings.TrimSuffix(cmd, suffix), true
	}
	return cmd, false
}

// rcWrap gives a fixture's (output, error) the shape a real TV shell returns with the marker.
func rcWrap(args []string, out string, err error) (string, error) {
	if len(args) > 1 && args[0] == "shell" {
		if _, marked := shellInner(args[1]); marked {
			rc := 0
			if err != nil {
				rc = 1
			}
			if out != "" {
				out += "\n"
			}
			return out + rcMarker + fmt.Sprint(rc), nil
		}
	}
	return out, err
}

func (f *fakeADB) Run(serial string, args ...string) (string, error) {
	out, err := f.run(serial, args...)
	return rcWrap(args, out, err)
}

// bootFocus models what a cold boot shows: our client when it is installed and is HOME or guarded.
func (f *fakeADB) bootFocus() string {
	if f.installed && (f.home == activity || strings.Contains(f.accessible, guard)) {
		return activity
	}
	if strings.HasPrefix(f.home, pkg+"/") {
		return "com.vendor/.Home"
	}
	return f.home
}

func (f *fakeADB) run(serial string, args ...string) (string, error) {
	f.calls = append(f.calls, serial+" "+strings.Join(args, " "))
	if len(args) == 0 {
		return "", nil
	}
	switch args[0] {
	case "version":
		return "Android Debug Bridge version 1.0.41\nVersion 37.0.1-test", nil
	case "connect":
		return "connected", nil
	case "get-state":
		return "device", nil
	case "devices":
		return "List of devices attached\n192.168.4.62:5555 device", nil
	case "mdns":
		return "", nil
	case "pair":
		return "Successfully paired", nil
	case "reboot":
		f.bootID++
		f.focus = f.bootFocus()
		return "", nil
	case "push":
		f.tvFiles[args[len(args)-1]] = true
		return "1 file pushed", nil
	case "pull":
		b := f.previousAPK
		if b == nil {
			b = []byte("original-test-apk")
		}
		if f.installedAPK != nil {
			b = f.installedAPK
		}
		os.WriteFile(args[2], b, 0600)
		return "pulled", nil
	case "install", "install-multiple":
		if f.rejectInstall {
			return "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]", errors.New("exit1")
		}
		f.installed = true
		b, e := os.ReadFile(args[len(args)-1])
		if e != nil {
			b = []byte("missing-payload")
		}
		f.installedAPK = b
		return "Success", nil
	case "shell":
		s, _ := shellInner(args[1])
		parts := strings.Split(strings.TrimSuffix(strings.TrimPrefix(s, "'"), "'"), "' '")
		has := func(prefix ...string) bool {
			return len(parts) >= len(prefix) && strings.Join(parts[:len(prefix)], "|") == strings.Join(prefix, "|")
		}
		switch {
		case has("getprop"):
			if len(parts) > 1 {
				if parts[1] == "sys.boot_completed" {
					return "1", nil
				}
				return "", nil
			}
			brand, device, fp := f.brand, f.device, f.fingerprint
			if brand == "" {
				brand = "Test"
			}
			if device == "" {
				device = "test-chip"
			}
			if fp == "" {
				fp = "test/fingerprint"
			}
			return "[ro.product.brand]: [" + brand + "]\n[ro.product.model]: [" + f.model + "]\n[ro.product.device]: [" + device + "]\n[ro.build.version.sdk]: [28]\n[ro.build.fingerprint]: [" + fp + "]\n[ro.build.version.incremental]: [1]", nil
		case has("cat", "/proc/sys/kernel/random/boot_id"):
			return fmt.Sprintf("boot-%d", f.bootID), nil
		case has("ls"):
			if f.tvFiles[parts[1]] {
				return parts[1], nil
			}
			return "No such file or directory", errors.New("exit1")
		case has("settings", "get", "secure", "android_id"):
			return f.identity, nil
		case has("settings", "get", "secure", "enabled_accessibility_services"):
			return f.accessible, nil
		case has("settings", "get", "secure", "accessibility_enabled"):
			return "1", nil
		case has("settings", "get", "secure", "enabled_notification_listeners"):
			return "com.voice/.Notifications", nil
		case has("settings", "put", "secure", "enabled_accessibility_services"):
			f.accessible = parts[4]
			return "", nil
		case has("settings", "delete", "secure", "enabled_accessibility_services"):
			f.accessible = ""
			return "", nil
		case has("settings", "get", "system", "screen_saver_timeout"), has("settings", "get", "secure", "screensaver_enabled"):
			if value, ok := f.nativeIdle[parts[2]+"/"+parts[3]]; ok {
				return value, nil
			}
			return "null", nil
		case has("settings", "put", "system", "screen_saver_timeout"), has("settings", "put", "secure", "screensaver_enabled"):
			if f.nativeIdle == nil {
				f.nativeIdle = map[string]string{}
			}
			f.nativeIdle[parts[2]+"/"+parts[3]] = parts[4]
			return "", nil
		case has("settings", "delete", "system", "screen_saver_timeout"), has("settings", "delete", "secure", "screensaver_enabled"):
			delete(f.nativeIdle, parts[2]+"/"+parts[3])
			return "Deleted 1 rows", nil
		case has("cmd", "package", "resolve-activity"), has("pm", "resolve-activity"):
			return f.home, nil
		case has("cmd", "package", "set-home-activity"), has("pm", "set-home-activity"):
			f.home = parts[len(parts)-1]
			return "", nil
		case has("pm", "uninstall"):
			if !f.installed {
				return "Failure [DELETE_FAILED_INTERNAL_ERROR]", errors.New("exit1")
			}
			f.installed, f.installedAPK = false, nil
			if strings.HasPrefix(f.home, pkg+"/") {
				f.home = "com.vendor/.Home"
			}
			return "Success", nil
		case has("pm", "path"):
			if f.installed {
				return "package:/data/app/example/base.apk", nil
			}
			return "", errors.New("exit status 1")
		case has("pm", "list", "packages"):
			out := "package:android\npackage:com.vendor.home"
			if f.installed {
				out += "\npackage:" + pkg
			}
			return out, nil
		case has("pm", "grant"):
			if f.rejectGrant {
				return "SecurityException", errors.New("denied")
			}
			return "", nil
		case has("am", "broadcast"):
			if strings.Contains(s, "provision_status") {
				if f.missingConfig {
					return "Broadcast completed: result=200, data=hotel_configured=false pin_configured=false", nil
				}
				return fmt.Sprintf("Broadcast completed: result=200, data=hotel_configured=%t pin_configured=%t", f.configured, f.pinConfigured), nil
			}
			if strings.Contains(s, "provision_rollback") {
				return "Broadcast completed: result=200, data=hotel_provisioning_restored", nil
			}
			if strings.Contains(s, "provision_server") || strings.Contains(s, "staff_pin_sha256") || strings.Contains(s, "legacy_defaults") {
				if f.rejectProvision {
					return "Broadcast completed: result=400", nil
				}
				if strings.Contains(s, "provision_server") || strings.Contains(s, "legacy_defaults") {
					f.configured = true
				}
				if strings.Contains(s, "staff_pin_sha256") || strings.Contains(s, "legacy_defaults") {
					f.pinConfigured = true
				}
				return "Broadcast completed: result=200, data=hotel_provisioned", nil
			}
			return "Broadcast completed: result=0", nil
		case has("am", "start"):
			f.focus = parts[len(parts)-1]
			return "Status: ok", nil
		case has("input", "keyevent"):
			f.focus = f.home
			if f.failHome {
				f.focus = "com.vendor/.Home"
			}
			return "", nil
		case has("dumpsys", "window"):
			return "mCurrentFocus=Window{a u0 " + f.focus + "}", nil
		default:
			return "", nil
		}
	}
	return "", nil
}
func testEngine(t *testing.T, f *fakeADB) Engine {
	t.Helper()
	e := Engine{Runner: f, Root: t.TempDir(), State: t.TempDir(), Wait: func(time.Duration) {}, Catalog: Catalog{Schema: 1, Profiles: []Profile{{ID: "hotel-test-tv", Name: "test", Route: "standard", Match: Match{Brand: "Test", Models: []string{"TestTV"}, Devices: []string{"test-chip"}, Min: 28, Max: 28, Fingerprint: "test/fingerprint"}}}}}
	e.CheckServer = func(string, string) (ServerCheck, error) {
		return ServerCheck{Items: 2, Message: "测试影院与设备凭证验证通过"}, nil
	}
	b := f.previousAPK
	if b == nil {
		b = []byte("original-test-apk")
	}
	os.MkdirAll(filepath.Join(e.Root, "payload"), 0700)
	os.WriteFile(filepath.Join(e.Root, "payload", "hotel-standard.apk"), []byte("payload-standard"), 0600)
	os.WriteFile(filepath.Join(e.Root, "payload", "hotel-guard.apk"), []byte("payload-guard"), 0600)
	atomicJSON(filepath.Join(e.Root, "payload", "upgrade-policy.json"), map[string]any{"schema": 1, "allowed_apk_sha256": []string{digest(b), digest([]byte("payload-standard")), digest([]byte("payload-guard"))}})
	return e
}
func request() InstallRequest {
	return InstallRequest{Target: "192.168.4.62", Server: "http://192.168.4.185:18790", Token: "unique-device-token-123456"}
}
func hash() string { return strings.Repeat("a", 64) }

func TestTargetsAndShellInjection(t *testing.T) {
	for _, raw := range []string{"8.8.8.8", "192.168.4.62;reboot", "localhost", "192.168.4.62:0", "192.168.4.62:65536", "192.168.4.62\nreboot"} {
		if _, e := validTarget(raw); e == nil {
			t.Fatal(raw)
		}
	}
	if s, e := validTarget("192.168.4.62:37001"); e != nil || s != "192.168.4.62:37001" {
		t.Fatal(s, e)
	}
	if quote("a'b") != "'a'\"'\"'b'" {
		t.Fatal("unsafe quoting")
	}
}
func TestIdentityMatchRequiresModelAndChip(t *testing.T) {
	e := testEngine(t, fake())
	if !decide(e.Catalog, Facts{Brand: "Test", Model: "TestTV", Device: "test-chip", SDK: 28, Fingerprint: "test/fingerprint"}).Allowed {
		t.Fatal("known model blocked")
	}
	for _, f := range []Facts{{Brand: "Test", Model: "TestTV", Device: "other", SDK: 28, Fingerprint: "test/fingerprint"}, {Brand: "Test", Model: "Unknown", Device: "test-chip", SDK: 28, Fingerprint: "test/fingerprint"}, {Brand: "Test", Model: "TestTV", Device: "test-chip", SDK: 29, Fingerprint: "test/fingerprint"}} {
		if d := decide(e.Catalog, f); d.Allowed || !d.Trial {
			t.Fatal("wrong variant accepted as verified (it may only be a trial)", f, d)
		}
	}
}
func TestUnknownModelCannotMutateTV(t *testing.T) {
	f := fake()
	f.model = "Unknown"
	e := testEngine(t, f)
	_, err := e.install(request(), hash(), func(string) {})
	if err == nil {
		t.Fatal("accepted unknown")
	}
	for _, c := range f.calls {
		if strings.Contains(c, " install ") || strings.Contains(c, "'put'") {
			t.Fatal(c)
		}
	}
}
func TestFreshInstallRequiresPinOnly(t *testing.T) {
	for _, r := range []InstallRequest{{Target: "192.168.4.62"}, request()} {
		f := fake()
		e := testEngine(t, f)
		_, err := e.install(r, "", func(string) {})
		if err == nil {
			t.Fatal("accepted unconfigured TV")
		}
		for _, c := range f.calls {
			if strings.Contains(c, " install ") {
				t.Fatal(c)
			}
		}
	}
}
func TestSuccessfulInstallPreservesVendorServicesAndNoSecretsInArtifacts(t *testing.T) {
	f := fake()
	e := testEngine(t, f)
	var logs []string
	s, err := e.install(request(), hash(), func(l string) { logs = append(logs, l) })
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(f.accessible, "com.voice/.Reader") || !strings.Contains(f.accessible, guard) {
		t.Fatal(f.accessible)
	}
	b, err := os.ReadFile(filepath.Join(e.State, "backups", s.ID, "snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), request().Token) || strings.Contains(strings.Join(logs, "\n"), request().Token) {
		t.Fatal("credential logged")
	}
	for _, c := range f.calls {
		if strings.Contains(c, "disable-user") || strings.Contains(c, "uninstall") || strings.Contains(c, "kill-server") {
			t.Fatal(c)
		}
	}
}
func TestHomeFailureRestoresOldAPKAndVendorHome(t *testing.T) {
	f := fake()
	f.installed = true
	f.failHome = true
	e := testEngine(t, f)
	s, err := e.install(request(), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "已执行恢复") {
		t.Fatal(err)
	}
	if f.home != "com.vendor/.Home" || f.accessible != "com.voice/.Reader" {
		t.Fatal(f.home, f.accessible)
	}
	if len(s.APKs) != 1 {
		t.Fatal(s)
	}
	found := false
	for _, c := range f.calls {
		if strings.Contains(c, "install -r -d") {
			found = true
		}
	}
	if !found {
		t.Fatal("old APK not restored")
	}
}
func TestSignatureMismatchDoesNotUninstallOrGrant(t *testing.T) {
	f := fake()
	f.installed = true
	f.rejectInstall = true
	e := testEngine(t, f)
	_, err := e.install(request(), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "签名") {
		t.Fatal(err)
	}
	for _, c := range f.calls {
		if strings.Contains(c, "uninstall") || strings.Contains(c, "'grant'") || strings.Contains(c, "'set-home-activity'") {
			t.Fatal(c)
		}
	}
}
func TestDeniedPermissionTriggersRestore(t *testing.T) {
	f := fake()
	f.rejectGrant = true
	e := testEngine(t, f)
	_, err := e.install(request(), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "已执行恢复") {
		t.Fatal(err)
	}
	if f.home != "com.vendor/.Home" {
		t.Fatal(f.home)
	}
}
func TestProvisioningRejectionTriggersRestoreAndNeverReportsSuccess(t *testing.T) {
	f := fake()
	f.rejectProvision = true
	e := testEngine(t, f)
	_, err := e.install(request(), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "配置接口") {
		t.Fatal(err)
	}
	if f.focus != "com.vendor/.Home" {
		t.Fatal(f.focus)
	}
}
func TestRestoreRejectsReassignedIPAndDamagedAPKBeforeMutation(t *testing.T) {
	for _, mode := range []string{"identity", "apk"} {
		t.Run(mode, func(t *testing.T) {
			f := fake()
			f.installed = true
			e := testEngine(t, f)
			facts, _, err := e.detect("192.168.4.62")
			if err != nil {
				t.Fatal(err)
			}
			s, err := e.snapshot("192.168.4.62:5555", facts)
			if err != nil {
				t.Fatal(err)
			}
			if mode == "identity" {
				f.identity = "another-tv"
			} else {
				os.WriteFile(filepath.Join(e.State, "backups", s.ID, s.APKs[0]), []byte("damaged"), 0600)
			}
			f.calls = nil
			if e.restore(s) == nil {
				t.Fatal("restore accepted")
			}
			for _, c := range f.calls {
				if strings.Contains(c, "'put'") || strings.Contains(c, "'force-stop'") || strings.Contains(c, " install ") {
					t.Fatal(c)
				}
			}
		})
	}
}
func TestRestorePreservesServicesAddedAfterBackup(t *testing.T) {
	f := fake()
	e := testEngine(t, f)
	facts, _, _ := e.detect("192.168.4.62")
	s, err := e.snapshot("192.168.4.62:5555", facts)
	if err != nil {
		t.Fatal(err)
	}
	f.accessible = "com.voice/.Reader:com.new/.Accessibility:" + guard
	if err = e.restore(s); err != nil {
		t.Fatal(err)
	}
	if f.accessible != "com.voice/.Reader:com.new/.Accessibility" {
		t.Fatal(f.accessible)
	}
}
func newTestApp(t *testing.T) *App {
	e := testEngine(t, fake())
	return &App{Engine: e, Token: "test-local-session-secret", Host: "127.0.0.1:18880"}
}
func call(a *App, path, body, token, origin, host string) *httptest.ResponseRecorder {
	method := "GET"
	if body != "" {
		method = "POST"
	}
	r := httptest.NewRequest(method, "http://"+host+path, strings.NewReader(body))
	r.Host = host
	r.Header.Set("X-App-Token", token)
	if origin != "" {
		r.Header.Set("Origin", origin)
	}
	w := httptest.NewRecorder()
	a.handler().ServeHTTP(w, r)
	return w
}
func TestAPIRejectsCSRFAndDNSRebinding(t *testing.T) {
	a := newTestApp(t)
	for _, tc := range []struct {
		tok, origin, host string
		status            int
	}{{"", "", a.Host, 401}, {a.Token, "http://evil.example", a.Host, 403}, {a.Token, "", "evil.example", 403}, {a.Token, "http://" + a.Host, a.Host, 200}} {
		w := call(a, "/api/state", "", tc.tok, tc.origin, tc.host)
		if w.Code != tc.status {
			t.Fatal(w.Code, w.Body.String())
		}
	}
}
func TestAPIPinSavedAsHashOnly(t *testing.T) {
	a := newTestApp(t)
	w := call(a, "/api/pin", "{}", a.Token, "", a.Host)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	var got map[string]string
	json.Unmarshal(w.Body.Bytes(), &got)
	p := got["pin"]
	if len(p) != 6 {
		t.Fatal(p)
	}
	b, _ := os.ReadFile(filepath.Join(a.Engine.State, "hotel.json"))
	if strings.Contains(string(b), p) {
		t.Fatal("plaintext persisted")
	}
	w = call(a, "/api/state", "", a.Token, "", a.Host)
	if strings.Contains(w.Body.String(), a.hotel.PinHash) || strings.Contains(w.Body.String(), p) {
		t.Fatal("secret exposed in state")
	}
}
func TestBatchCannotReuseDeviceCredential(t *testing.T) {
	a := newTestApp(t)
	body := `{"devices":[{"target":"192.168.4.62","token":"same-token-123456789"},{"target":"192.168.4.63","token":"same-token-123456789"}]}`
	w := call(a, "/api/install", body, a.Token, "", a.Host)
	if w.Code != 400 || a.job != nil {
		t.Fatal(w.Code, w.Body.String())
	}
}
func TestOperationMutexStopsConcurrentProbe(t *testing.T) {
	a := newTestApp(t)
	a.op.Lock()
	defer a.op.Unlock()
	w := call(a, "/api/probe", `{"target":"192.168.4.62"}`, a.Token, "", a.Host)
	if w.Code != 400 {
		t.Fatal(w.Code)
	}
}
func TestSnapshotPathCannotEscape(t *testing.T) {
	e := testEngine(t, fake())
	for _, id := range []string{"../a", "/etc/passwd", strings.Repeat("a", 32) + "/../b"} {
		if _, err := e.loadSnapshot(id); err == nil {
			t.Fatal(id)
		}
	}
}
func TestPackageIntegrityAndTraversal(t *testing.T) {
	root := t.TempDir()
	os.Mkdir(filepath.Join(root, "payload"), 0700)
	os.Mkdir(filepath.Join(root, "helpers"), 0700)
	files := map[string]string{}
	for _, p := range []string{"catalog.json", "payload/hotel-standard.apk", "payload/hotel-guard.apk", "payload/upgrade-policy.json", "helpers/power-mode.jar"} {
		os.WriteFile(filepath.Join(root, p), []byte(p), 0600)
		files[p] = digest([]byte(p))
	}
	atomicJSON(filepath.Join(root, "manifest.json"), map[string]any{"files": files})
	if err := verifyBundle(root); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(root, "catalog.json"), []byte("corruption"), 0600)
	if verifyBundle(root) == nil {
		t.Fatal("tampered file accepted")
	}
	files["../../outside"] = "x"
	atomicJSON(filepath.Join(root, "manifest.json"), map[string]any{"files": files})
	if verifyBundle(root) == nil {
		t.Fatal("traversal accepted")
	}
}
func TestFirmwareSpecificRecordCannotMatchOtherBuild(t *testing.T) {
	e := testEngine(t, fake())
	e.Catalog.Profiles[0].Match.Fingerprint = "exact-firmware"
	f := Facts{Brand: "Test", Model: "TestTV", Device: "test-chip", SDK: 28, Fingerprint: "another-build"}
	if decide(e.Catalog, f).Allowed {
		t.Fatal("firmware mismatch accepted")
	}
}
func TestProfileUpdateCannotIntroduceExecutableRoute(t *testing.T) {
	path := filepath.Join(t.TempDir(), "catalog.json")
	os.WriteFile(path, []byte(`{"schema":1,"profiles":[{"id":"evil","route":"curl | sh"}]}`), 0600)
	if _, e := loadCatalog(path); e == nil {
		t.Fatal("arbitrary route accepted")
	}
}

func TestSingleInstanceLockReleasesAfterExit(t *testing.T) {
	path := filepath.Join(t.TempDir(), "instance.lock")
	f, e := lockState(path)
	if e != nil {
		t.Fatal(e)
	}
	if f2, e2 := lockState(path); e2 == nil {
		f2.Close()
		t.Fatal("second instance accepted")
	}
	f.Close()
	f3, e := lockState(path)
	if e != nil {
		t.Fatal("lock not released", e)
	}
	f3.Close()
}

func TestUpstreamFirmwarePrefixMustMatch(t *testing.T) {
	e := testEngine(t, fake())
	e.Catalog.Profiles = append(e.Catalog.Profiles, Profile{ID: "upstream-test", Name: "upstream", Route: "standard", Match: Match{Brand: "Test", Models: []string{"OtherTV"}, Min: 28, Max: 28, FingerprintPrefix: "Test/verified/"}})
	f := Facts{Brand: "Test", Model: "OtherTV", Device: "x", SDK: 28, Fingerprint: "Test/other/release"}
	if d := decide(e.Catalog, f); d.Profile != nil || d.Allowed {
		t.Fatal("ignored upstream firmware constraint", d)
	}
	f.Fingerprint = "Test/verified/20261028"
	d := decide(e.Catalog, f)
	if d.Profile == nil || d.Profile.ID != "upstream-test" || d.Allowed || !d.Trial || d.Tier != "trial" {
		t.Fatal("upstream record must be a trial, never one-click", d)
	}
}

func TestRestartLoadsInterruptedTaskWithoutBlockingRecovery(t *testing.T) {
	state := t.TempDir()
	id := strings.Repeat("c", 32)
	atomicJSON(filepath.Join(state, "reports", id+".json"), Job{ID: id, State: "running", Lines: []string{"backup saved"}})
	j := latestJob(state)
	if j == nil || j.ID != id || j.State != "interrupted" || len(j.Lines) != 2 {
		t.Fatal(j)
	}
	a := &App{job: j}
	if a.busy() {
		t.Fatal("interrupted task blocks recovery")
	}
	var saved Job
	b, _ := os.ReadFile(filepath.Join(state, "reports", id+".json"))
	json.Unmarshal(b, &saved)
	if saved.State != "interrupted" {
		t.Fatal(saved.State)
	}
}
func TestInvalidReportCannotBreakStartup(t *testing.T) {
	state := t.TempDir()
	path := filepath.Join(state, "reports", strings.Repeat("d", 32)+".json")
	os.MkdirAll(filepath.Dir(path), 0700)
	os.WriteFile(path, []byte("broken json"), 0600)
	if latestJob(state) != nil {
		t.Fatal("invalid report loaded")
	}
}

func TestUpgradeUnknownClientRefusedBeforeReplacingAnyAPK(t *testing.T) {
	f := fake()
	f.installed = true
	e := testEngine(t, f)
	atomicJSON(filepath.Join(e.Root, "payload", "upgrade-policy.json"), map[string]any{"schema": 1, "allowed_apk_sha256": []string{hash()}})
	_, err := e.install(request(), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "可覆盖清单") {
		t.Fatal(err)
	}
	for _, c := range f.calls {
		if strings.Contains(c, " install ") || strings.Contains(c, "'put'") {
			t.Fatal("mutated unknown client")
		}
	}
}
func TestUpgradeMigratesLegacyDefaultsWhenOperatorLeavesConnectionBlank(t *testing.T) {
	f := fake()
	f.installed = true
	var data bytes.Buffer
	z := zip.NewWriter(&data)
	entry, _ := z.Create("assets/hotel-defaults.json")
	entry.Write([]byte(`{"server":"http://192.168.4.185:18790","device_token":"legacy-test-credential-123","staff_pin_sha256":"` + hash() + `"}`))
	z.Close()
	f.previousAPK = data.Bytes()
	e := testEngine(t, f)
	_, err := e.install(InstallRequest{Target: "192.168.4.62"}, "", func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	sent := false
	for _, c := range f.calls {
		if strings.Contains(c, "legacy_defaults") {
			sent = true
		}
	}
	if !sent {
		t.Fatal("legacy defaults were not passed to conditional migration")
	}
}
func TestMissingConfigurationCannotBeReportedAsSuccess(t *testing.T) {
	f := fake()
	f.missingConfig = true
	e := testEngine(t, f)
	_, err := e.install(request(), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "未确认服务器") {
		t.Fatal(err)
	}
}
