package main

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestServerPreflightUsesTVAuthAndRejectsWrongPortAndRedirect(t *testing.T) {
	const token = "test-only-credential-123456"
	received := false
	destination := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { received = true }))
	defer destination.Close()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/catalog" || r.Header.Get("X-TV-Token") != token {
			t.Error("wrong TV endpoint or auth")
		}
		switch r.Header.Get("X-Test-Case") {
		default:
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"items":[{"id":"film"}]}`))
		}
	}))
	defer s.Close()
	check, err := checkServer(s.URL, token)
	if err != nil || check.Items != 1 {
		t.Fatal(check, err)
	}
	for _, kind := range []string{"denied", "html", "null", "redirect", "large"} {
		t.Run(kind, func(t *testing.T) {
			bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch kind {
				case "denied":
					w.WriteHeader(403)
					w.Write([]byte(token))
				case "html":
					w.Write([]byte("<html>admin</html>"))
				case "null":
					w.Write([]byte(`{"items":null}`))
				case "redirect":
					http.Redirect(w, r, destination.URL, 302)
				case "large":
					w.Write([]byte(strings.Repeat(" ", 4<<20) + `{"items":[]}`))
				}
			}))
			defer bad.Close()
			_, err := checkServer(bad.URL, token)
			if err == nil || strings.Contains(err.Error(), token) {
				t.Fatal("accepted bad endpoint or leaked credential", err)
			}
		})
	}
	if received {
		t.Fatal("followed redirect and exposed device credential")
	}
}

func TestServerAvailabilityDoesNotBlockInstallation(t *testing.T) {
	f := fake()
	e := testEngine(t, f)
	checks := 0
	e.CheckServer = func(string, string) (ServerCheck, error) {
		checks++
		return ServerCheck{}, errors.New("server not online yet")
	}
	s, err := e.install(request(), hash(), func(string) {})
	if err != nil || !s.ServerConfigured || checks != 0 {
		t.Fatal(s, err, "network check should be optional", checks)
	}
}

type delayedHome struct {
	*fakeADB
	reads   int
	waiting bool
}

func (f *delayedHome) Run(serial string, args ...string) (string, error) {
	out, err := f.fakeADB.Run(serial, args...)
	if len(args) > 1 && args[0] == "shell" {
		if strings.Contains(args[1], "'input' 'keyevent'") {
			f.waiting = true
			f.reads = 0
		}
		if strings.Contains(args[1], "'dumpsys' 'window'") && f.waiting {
			f.reads++
			if f.reads < 6 {
				return rcWrap(args, "mCurrentFocus=Window{a u0 com.vendor/.Home}", nil)
			}
			f.waiting = false
		}
	}
	return out, err
}
func TestHomeWaitsForRealFocusInsteadOfFixedThreeSeconds(t *testing.T) {
	f := fake()
	e := testEngine(t, f)
	d := &delayedHome{fakeADB: f}
	e.Runner = d
	if _, err := e.install(request(), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if d.reads != 6 {
		t.Fatal("did not poll through system app-switch delay", d.reads)
	}
}

type failedConfigRollback struct{ *fakeADB }

func (f failedConfigRollback) Run(serial string, args ...string) (string, error) {
	if len(args) > 1 && args[0] == "shell" && strings.Contains(args[1], "provision_rollback") {
		return rcWrap(args, "Broadcast completed: result=400", nil)
	}
	return f.fakeADB.Run(serial, args...)
}
func TestRestoreCannotClaimConfigurationRollbackFromExitZero(t *testing.T) {
	f := fake()
	f.installed = true
	e := testEngine(t, f)
	s, err := e.install(request(), hash(), func(string) {})
	if err != nil {
		t.Fatal(err)
	}
	f.calls = nil
	e.Runner = failedConfigRollback{f}
	if err = e.restore(s); err == nil || !strings.Contains(err.Error(), "配置回退未确认") {
		t.Fatal(err)
	}
	for _, c := range f.calls {
		if strings.Contains(c, " install ") {
			t.Fatal("replaced APK before private configuration rollback", c)
		}
	}
	if f.focus != "com.vendor/.Home" {
		t.Fatal("did not restore vendor foreground")
	}
}

func TestAttachmentAllowlistAndAuthentication(t *testing.T) {
	e := testEngine(t, fake())
	os.MkdirAll(filepath.Join(e.Root, "extras"), 0700)
	os.WriteFile(filepath.Join(e.Root, "extras", "android40-experiment.zip"), []byte("test zip"), 0600)
	a := &App{Engine: e, Host: "127.0.0.1:12345", Token: "session"}
	for _, id := range []string{"../../data/hotel.json", "unknown"} {
		r := httptest.NewRequest("GET", "http://"+a.Host+"/api/attachment?id="+id, nil)
		r.Header.Set("X-App-Token", a.Token)
		w := httptest.NewRecorder()
		a.handler().ServeHTTP(w, r)
		if w.Code != 400 {
			t.Fatal(w.Code)
		}
	}
	r := httptest.NewRequest("GET", "http://"+a.Host+"/api/attachment?id=ics", nil)
	w := httptest.NewRecorder()
	a.handler().ServeHTTP(w, r)
	if w.Code != 401 {
		t.Fatal("attachment bypassed auth")
	}
	r.Header.Set("X-App-Token", a.Token)
	w = httptest.NewRecorder()
	a.handler().ServeHTTP(w, r)
	if w.Code != 200 || w.Body.String() != "test zip" {
		t.Fatal(w.Code, w.Body.String())
	}
}

func TestExistingPinIsPersistedOnlyAsHash(t *testing.T) {
	a := &App{Engine: testEngine(t, fake()), Host: "127.0.0.1:12345", Token: "session"}
	for _, pin := range []string{"12345", "abc123", "012345"} {
		r := httptest.NewRequest("POST", "http://"+a.Host+"/api/pin", strings.NewReader(`{"pin":"`+pin+`"}`))
		r.Header.Set("X-App-Token", a.Token)
		w := httptest.NewRecorder()
		a.handler().ServeHTTP(w, r)
		if pin != "012345" {
			if w.Code != 400 {
				t.Fatal("accepted invalid PIN")
			}
			continue
		}
		b, err := os.ReadFile(filepath.Join(a.Engine.State, "hotel.json"))
		if err != nil || strings.Contains(string(b), pin) || a.hotel.PinHash != digest([]byte(pin)) {
			t.Fatal("did not hash existing PIN")
		}
	}
}

type ignoredGuardWrite struct{ *fakeADB }

func (f ignoredGuardWrite) Run(serial string, args ...string) (string, error) {
	if len(args) > 1 && args[0] == "shell" && strings.HasPrefix(args[1], "'settings' 'put' 'secure' 'enabled_accessibility_services'") {
		return rcWrap(args, "", nil)
	}
	return f.fakeADB.Run(serial, args...)
}
func TestGuardWriteExitZeroRequiresActualReadback(t *testing.T) {
	f := fake()
	e := testEngine(t, f)
	e.Runner = ignoredGuardWrite{f}
	_, err := e.install(request(), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "读回未确认") {
		t.Fatal("accepted ignored guard setting", err)
	}
	if strings.Contains(f.accessible, guard) {
		t.Fatal("fixture accidentally applied the denied binding")
	}
}
