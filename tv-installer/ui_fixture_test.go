package main

import (
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type uiFixtureRunner struct {
	*fakeADB
	power *powerRunner
}

func (f uiFixtureRunner) Run(serial string, args ...string) (string, error) {
	if strings.HasPrefix(serial, "192.168.4.98:") && len(args) > 1 && args[0] == "shell" && strings.Contains(args[1], "'PowerModeTool'") {
		return rcWrap(args, "MODE_BEFORE=unknown", nil)
	}
	if f.power != nil && len(args) > 1 && args[0] == "shell" && strings.Contains(args[1], "'PowerModeTool'") {
		return f.power.Run(serial, args...)
	}
	if strings.HasPrefix(serial, "192.168.4.99:") && len(args) > 1 && args[0] == "shell" && strings.HasPrefix(args[1], "'getprop'; ") {
		return rcWrap(args, "[ro.product.brand]: [Unknown]\n[ro.product.model]: [Unlisted-TV]\n[ro.product.device]: [unknown-chip]\n[ro.build.version.sdk]: [28]\n[ro.build.fingerprint]: [unknown/build]", nil)
	}
	return f.fakeADB.Run(serial, args...)
}

// This server is part of the test binary only. It never invokes a real ADB process.
func TestUIFixture(t *testing.T) {
	if os.Getenv("TI_UI_FIXTURE") != "1" {
		t.Skip("browser test fixture disabled")
	}
	f := fake()
	f.model = "MiTV-ASTP0"
	f.brand = "Xiaomi"
	f.device = "mulan"
	f.fingerprint = "Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys"
	e := testEngine(t, f)
	e.Runner = uiFixtureRunner{fakeADB: f, power: &powerRunner{fakeADB: f, mode: "secondary"}}
	helper := filepath.Join(e.Root, "helpers", "power-mode.jar")
	os.MkdirAll(filepath.Dir(helper), 0700)
	os.WriteFile(helper, []byte("ui-fixture-dex"), 0600)
	if err := atomicJSON(filepath.Join(e.Root, "manifest.json"), map[string]any{"files": map[string]string{"helpers/power-mode.jar": digest([]byte("ui-fixture-dex"))}}); err != nil {
		t.Fatal(err)
	}
	c, err := loadCatalog("catalog.json")
	if err != nil {
		t.Fatal(err)
	}
	e.Catalog = c
	e.CheckServer = func(origin, token string) (ServerCheck, error) {
		if strings.HasPrefix(token, "offline-test-") {
			return ServerCheck{}, errors.New("服务器暂不可达，仍可继续安装")
		}
		return ServerCheck{Items: 2, Message: "测试影院与设备凭证验证通过"}, nil
	}
	os.MkdirAll(filepath.Join(e.Root, "extras"), 0700)
	for _, row := range e.attachments() {
		os.WriteFile(filepath.Join(e.Root, "extras", row.File), []byte("test-only-attachment"), 0600)
	}
	// Backend login goes to a fake admin API on a fixed port (type 127.0.0.1:18948, password
	// fixture-pass); registration then uses an in-memory backend.
	admin := &http.Server{Handler: &fakeAdmin{role: "admin", password: "fixture-pass"}}
	al, err := net.Listen("tcp4", "127.0.0.1:18948")
	if err != nil {
		t.Fatal(err)
	}
	go admin.Serve(al)
	defer admin.Close()
	e.Backend = newFakeBackend(nil)
	a := &App{Engine: e, Token: "ti-test-fixture-session", Host: "127.0.0.1:18947", backend: &Backend{}, allowLoopbackBackend: true}
	if os.Getenv("TI_MOBILE_FIXTURE") == "1" {
		a.Pairing = &BrowserPairing{Code: "12345678", Expires: time.Now().Add(time.Hour), now: time.Now}
	}
	ln, err := net.Listen("tcp4", a.Host)
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	t.Log("UI fixture only at http://127.0.0.1:18947/#ti-test-fixture-session")
	if err = http.Serve(ln, a.handler()); err != nil {
		t.Fatal(err)
	}
}
