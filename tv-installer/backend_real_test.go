package main

import (
	"net/http"
	"os"
	"strings"
	"testing"
)

// TestBackendAgainstRealServer runs the registration against the project's own server.py, started
// locally on a throw-away data folder (never the hotel's server):
//
//	TI_REAL_BACKEND=127.0.0.1:28791 TI_REAL_USER=admin TI_REAL_PASS=... go test -run RealServer
func TestBackendAgainstRealServer(t *testing.T) {
	addr := os.Getenv("TI_REAL_BACKEND")
	if addr == "" {
		t.Skip("local server.py not configured")
	}
	user, pass := os.Getenv("TI_REAL_USER"), os.Getenv("TI_REAL_PASS")
	b := &Backend{}
	if _, err := b.Login(addr, user, pass+"-wrong"); err == nil || !strings.Contains(err.Error(), "后台返回") {
		t.Fatal("wrong password not reported with the backend's own message:", err)
	}
	if b.Status().LoggedIn {
		t.Fatal("logged in after a failed login")
	}
	st, err := b.Login(addr, user, pass)
	if err != nil {
		t.Fatal(err)
	}
	if !st.LoggedIn || st.TV != "http://127.0.0.1:28790" || st.Admin != "http://127.0.0.1:28791" {
		t.Fatalf("status %+v", st)
	}
	if err = b.Ready(); err != nil {
		t.Fatal(err)
	}
	id := backendDeviceID("real-server-test-" + randomTestSuffix(t))
	if _, found, err := b.Lookup(id, ""); err != nil || found {
		t.Fatal("new id found before registering", found, err)
	}
	token, _, created, err := b.Register(id, "待命名-227", "Xiaomi MiTV-ASTP0")
	if err != nil || !created || !tokenFormat.MatchString(token) {
		t.Fatal("create", created, err)
	}
	// The tool's own check is a probe: it proves the credential without making the TV look online.
	if _, err = checkServer(b.TVOrigin(), token); err != nil {
		t.Fatal("PC-side check with the issued credential:", err)
	}
	if d, err := b.Seen(id); err != nil || d.LastSeen != 0 || d.Online || d.Room != "待命名-227" {
		t.Fatalf("probe counted as the TV coming online: %+v %v", d, err)
	}
	tvFetch(t, b.TVOrigin(), token, 200) // the TV itself
	d, err := b.Seen(id)
	if err != nil || d.LastSeen == 0 || !d.Online || d.IP != "127.0.0.1" {
		t.Fatalf("TV contact not seen: %+v %v", d, err)
	}
	if found, ok, err := b.Lookup("tv-0000000000000000", "127.0.0.1"); err != nil || !ok || found.ID != id {
		t.Fatal("online device at this address not found", found, ok, err)
	}
	if _, ok, _ := b.Lookup(id, "10.9.9.9"); ok {
		t.Fatal("entry matched by id although the backend hears from it at another address")
	}
	// Registering the same TV again issues a new credential and kills the old one; the room name
	// staff may have set is kept.
	again, before, created, err := b.Register(id, "别的名字", "x")
	if err != nil || created || again == token || before.LastSeen != d.LastSeen || before.Room != "待命名-227" {
		t.Fatal("re-register", created, err, before)
	}
	tvFetch(t, b.TVOrigin(), token, 401)
	tvFetch(t, b.TVOrigin(), again, 200)
	if d, _ := b.Seen(id); d.Room != "待命名-227" {
		t.Fatal("room renamed by a re-register", d.Room)
	}
	// Staff put it in the recycle bin: the tool does not revive it.
	if err = b.archive(id); err != nil {
		t.Fatal(err)
	}
	if d, found, _ := b.Lookup(id, "127.0.0.1"); found && d.ID == id {
		t.Fatal("archived device still looked up")
	}
	if _, _, _, err = b.Register(id, "x", "x"); err == nil || !strings.Contains(err.Error(), "回收站") {
		t.Fatal("archived device re-registered silently", err)
	}
	// An entry discarded after a rolled-back install is gone, and the TV registers afresh.
	other := backendDeviceID("real-server-discard-" + randomTestSuffix(t))
	if _, _, _, err = b.Register(other, "x", "x"); err != nil {
		t.Fatal(err)
	}
	if err = b.Discard(other); err != nil {
		t.Fatal("discard", err)
	}
	if _, _, created, err := b.Register(other, "y", "x"); err != nil || !created {
		t.Fatal("register after discard", created, err)
	}
	b.Discard(other)
	b.Logout()
	if err = b.Ready(); err == nil || b.Status().LoggedIn {
		t.Fatal("still usable after logout")
	}
	// A session ended on the server side (12 h, or logged out in the backend) is reported as such.
	if _, err = b.Login(addr, user, pass); err != nil {
		t.Fatal(err)
	}
	elsewhere := &Backend{}
	elsewhere.admin, elsewhere.cookie, elsewhere.csrf = b.admin, b.cookie, b.csrf
	elsewhere.Logout()
	if err = b.Ready(); err != errBackendSession {
		t.Fatal("server-side logout", err)
	}
	if _, _, _, err = b.Register(backendDeviceID("x"), "x", "x"); err == nil {
		t.Fatal("registered without a session")
	}
}

func tvFetch(t *testing.T, origin, token string, want int) {
	t.Helper()
	req, _ := http.NewRequest("GET", origin+"/api/v1/catalog", nil)
	req.Header.Set("X-TV-Token", token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != want {
		t.Fatalf("TV request: HTTP %d, want %d", resp.StatusCode, want)
	}
}

func randomTestSuffix(t *testing.T) string {
	s, err := randomID()
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// tvClient makes the simulated TV behave like the client after its connection is saved: the next
// start fetches the catalog from the saved server with the saved credential.
type tvClient struct {
	*tvSim
	t *testing.T
}

func (c tvClient) Run(serial string, args ...string) (string, error) {
	out, err := c.tvSim.Run(serial, args...)
	if len(args) > 1 && args[0] == "shell" && strings.Contains(args[1], "'am' 'start'") && c.tvSim.provServer != "" {
		req, _ := http.NewRequest("GET", c.tvSim.provServer+"/api/v1/catalog", nil)
		req.Header.Set("X-TV-Token", c.tvSim.provToken)
		if resp, e := http.DefaultClient.Do(req); e == nil {
			resp.Body.Close()
		}
	}
	return out, err
}

func TestInstallRegistersWithTheRealServer(t *testing.T) {
	addr := os.Getenv("TI_REAL_BACKEND")
	if addr == "" {
		t.Skip("local server.py not configured")
	}
	b := &Backend{}
	if _, err := b.Login(addr, os.Getenv("TI_REAL_USER"), os.Getenv("TI_REAL_PASS")); err != nil {
		t.Fatal(err)
	}
	defer b.Logout()
	s := newSim(t, "real-backend-"+randomTestSuffix(t))
	e := simEngine(t, s, true)
	e.Runner, e.Backend = tvClient{s, t}, b
	var logs []string
	// The simulated TV's requests come from this machine, so that is its address here.
	req := registerRequest("")
	req.Target = "127.0.0.1"
	snap, err := e.install(req, hash(), func(l string) { logs = append(logs, l) })
	if err != nil {
		t.Fatal(err, "\n", strings.Join(logs, "\n"))
	}
	if !snap.ServerOnline || s.provServer != "http://127.0.0.1:28790" {
		t.Fatal("not confirmed online through the real backend", snap.ServerOnline, s.provServer, "\n", strings.Join(logs, "\n"))
	}
	d, err := b.Seen(snap.BackendDevice)
	if err != nil || d.Room != "待命名-1" || !d.Online {
		t.Fatalf("%+v %v", d, err)
	}
	// Second run on the same TV: kept, no new credential.
	s.adbOpen = true
	token := s.provToken
	if snap, err = e.install(req, hash(), func(string) {}); err != nil || s.provToken != token || !snap.ServerOnline || !snap.BackendKept {
		t.Fatal("re-install against the real backend", err, s.provToken == token, snap.ServerOnline)
	}
	b.Discard(snap.BackendDevice)
}
