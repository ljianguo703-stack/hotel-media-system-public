package main

import (
	"errors"
	"strings"
	"testing"
)

// fakeBackend is the backend as seen by the install: registrations, credentials, and whether it has
// heard from the TV (the TV "connects" once its saved credential is the one issued).
type fakeBackend struct {
	sim         *tvSim
	tv          string
	devices     map[string]*fakeDevice
	readyErr    error
	failWith    error // Register fails
	tvOffline   bool  // the TV cannot reach the server
	discarded   []string
	seenFails   bool // Seen errors (the baseline must not depend on it)
	sessionLost bool // Seen reports the session ended
	registers   int
	tokens      int
}

type fakeDevice struct {
	BackendDevice
	token string
}

func newFakeBackend(s *tvSim) *fakeBackend {
	return &fakeBackend{sim: s, tv: "http://10.20.9.215:18790", devices: map[string]*fakeDevice{}}
}

func (f *fakeBackend) Ready() error     { return f.readyErr }
func (f *fakeBackend) TVOrigin() string { return f.tv }
func (f *fakeBackend) Lookup(id, ip string) (BackendDevice, bool, error) {
	f.touch()
	// Same rule as Backend.Lookup: only an entry the backend is hearing from at this address.
	if d, ok := f.devices[id]; ok && !d.Archived && d.Enabled && d.Online && d.IP == ip {
		return d.BackendDevice, true, nil
	}
	for _, d := range f.devices {
		if d.IP == ip && d.Online && !d.Archived && d.Enabled {
			return d.BackendDevice, true, nil
		}
	}
	return BackendDevice{}, false, nil
}
func (f *fakeBackend) newToken() string {
	f.tokens++
	return strings.Repeat("k", 30) + string(rune('A'+f.tokens))
}
func (f *fakeBackend) Register(id, room, model string) (string, BackendDevice, bool, error) {
	f.registers++
	if f.failWith != nil {
		return "", BackendDevice{}, false, f.failWith
	}
	if d, ok := f.devices[id]; ok {
		if d.Archived {
			return "", BackendDevice{}, false, errors.New("这台电视在后台回收站里")
		}
		before := d.BackendDevice
		d.token = f.newToken()
		return d.token, before, false, nil
	}
	d := &fakeDevice{BackendDevice: BackendDevice{ID: id, Room: room, Enabled: true}, token: f.newToken()}
	f.devices[id] = d
	return d.token, d.BackendDevice, true, nil
}

// touch: a TV whose saved connection points here with a valid credential contacts the server.
func (f *fakeBackend) touch() {
	if f.tvOffline || f.sim == nil || !f.sim.installed || f.sim.provServer != f.tv {
		return
	}
	for _, d := range f.devices {
		if d.token == f.sim.provToken && !d.Archived {
			d.LastSeen++
			d.Online, d.IP = true, "192.168.4.70"
		}
	}
}
func (f *fakeBackend) Seen(id string) (BackendDevice, error) {
	if f.sessionLost {
		return BackendDevice{}, errBackendSession
	}
	if f.seenFails {
		return BackendDevice{}, errors.New("timeout")
	}
	f.touch()
	if d, ok := f.devices[id]; ok && f.sim == nil && !f.tvOffline {
		d.LastSeen++ // browser fixture: no simulated TV, the backend hears from it at once
		d.Online = true
	}
	if d, ok := f.devices[id]; ok {
		return d.BackendDevice, nil
	}
	return BackendDevice{}, errors.New("设备不存在")
}
func (f *fakeBackend) Discard(id string) error {
	delete(f.devices, id)
	f.discarded = append(f.discarded, id)
	return nil
}

func registerEngine(t *testing.T, s *tvSim) (Engine, *fakeBackend) {
	e := simEngine(t, s, true)
	b := newFakeBackend(s)
	e.Backend = b
	e.CheckServer = func(string, string) (ServerCheck, error) { return ServerCheck{Items: 3}, nil }
	return e, b
}

func registerRequest(room string) InstallRequest {
	r := simRequest(false)
	r.Register, r.Room = true, room
	return r
}

func TestRegisterModeOnlyNeedsTheServerAndConfirmsTheTVIsOnline(t *testing.T) {
	s := newSim(t, "register-fresh")
	e, b := registerEngine(t, s)
	var logs []string
	snap, err := e.install(registerRequest(""), hash(), func(l string) { logs = append(logs, l) })
	if err != nil {
		t.Fatal(err, "\n", strings.Join(logs, "\n"))
	}
	id := backendDeviceID(digest([]byte(s.identity)))
	d := b.devices[id]
	if d == nil || d.Room != "待命名-70" || s.provServer != b.tv || s.provToken != d.token {
		t.Fatalf("TV not registered and given its own credential: %+v server=%q", d, s.provServer)
	}
	if !snap.ServerConfigured || !snap.ServerOnline || snap.BackendDevice != id || snap.BackendRoom != "待命名-70" {
		t.Fatalf("connection not confirmed: %+v", snap)
	}
	all := strings.Join(logs, "\n")
	if strings.Contains(all, d.token) {
		t.Fatal("credential written to the job log")
	}
	if !strings.Contains(all, "连接已确认") {
		t.Fatal(all)
	}
}

func TestRegisterModeUsesTheRoomNameTyped(t *testing.T) {
	s := newSim(t, "register-room")
	e, b := registerEngine(t, s)
	if _, err := e.install(registerRequest("301"), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if d := b.devices[backendDeviceID(digest([]byte(s.identity)))]; d == nil || d.Room != "301" {
		t.Fatal("room name not used", d)
	}
}

func TestReinstallKeepsTheConnectionAlreadyOnThisBackend(t *testing.T) {
	s := newSim(t, "register-again")
	e, b := registerEngine(t, s)
	if _, err := e.install(registerRequest(""), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	token := s.provToken
	var logs []string
	snap, err := e.install(registerRequest("别的"), hash(), func(l string) { logs = append(logs, l) })
	if err != nil {
		t.Fatal(err)
	}
	if b.registers != 1 || s.provToken != token || len(b.devices) != 1 {
		t.Fatal("re-install issued a new credential or a second entry", b.registers, len(b.devices))
	}
	if !snap.ServerOnline || !strings.Contains(strings.Join(logs, "\n"), "保留原连接") {
		t.Fatal("kept connection not confirmed", snap.ServerOnline, logs)
	}
}

func TestTVConnectedElsewhereIsMovedToThisBackendAndPutBackOnRollback(t *testing.T) {
	s := newSim(t, "register-elsewhere")
	e, b := registerEngine(t, s)
	manual := simRequest(false)
	manual.Server, manual.Token = "http://10.0.0.9:18790", strings.Repeat("o", 24)
	if _, err := e.install(manual, hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	if _, err := e.install(registerRequest(""), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if s.provServer != b.tv || b.registers != 1 {
		t.Fatal("TV connected to another server was not moved", s.provServer)
	}
	// A later failed run (the boot check fails) puts the previous connection back and the entry it
	// created is not left behind in the backend.
	u := newSim(t, "register-rollback")
	eu, bu := registerEngine(t, u)
	if _, err := eu.install(manual, hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	u.adbOpen = true
	// Only the verification boot of this run loses HOME (pre-check boot 1, verification 2, restore 3).
	eu.Runner = &bootFailsAt{tvSim: u, at: 2}
	_, err := eu.install(registerRequest(""), hash(), func(string) {})
	if err == nil || !strings.Contains(err.Error(), "已执行恢复") {
		t.Fatal("boot check failure not rolled back", err)
	}
	if u.provServer != "http://10.0.0.9:18790" || u.provToken != strings.Repeat("o", 24) {
		t.Fatal("previous connection not put back", u.provServer)
	}
	if len(bu.discarded) != 1 || len(bu.devices) != 0 {
		t.Fatal("entry created by the failed run left in the backend", bu.discarded)
	}
}

func TestFirstInstallRolledBackRemovesTheNewEntry(t *testing.T) {
	s := newSim(t, "register-first-fails")
	s.rebootLosesHome = true
	e, b := registerEngine(t, s)
	var logs []string
	_, err := e.install(registerRequest(""), hash(), func(l string) { logs = append(logs, l) })
	if err == nil || s.installed {
		t.Fatal("failed install not rolled back", err)
	}
	if len(b.discarded) != 1 || !strings.Contains(strings.Join(logs, "\n"), "已删除") {
		t.Fatal("new entry not removed after the rollback", b.discarded)
	}
	// A retry after the rollback registers again without anyone touching the backend.
	s.rebootLosesHome, s.adbOpen = false, true
	if snap, err := e.install(registerRequest(""), hash(), func(string) {}); err != nil || !snap.ServerOnline {
		t.Fatal("retry after a rolled-back install", err)
	}
}

func TestRotatedEntryIsNotArchivedOnRollback(t *testing.T) {
	// The TV was registered before (factory reset since): the entry is reused with a new credential
	// and must survive a failed run — staff may have named it.
	s := newSim(t, "register-rotate")
	e, b := registerEngine(t, s)
	id := backendDeviceID(digest([]byte(s.identity)))
	b.devices[id] = &fakeDevice{BackendDevice: BackendDevice{ID: id, Room: "305", Enabled: true}, token: "old"}
	s.rebootLosesHome = true
	if _, err := e.install(registerRequest(""), hash(), func(string) {}); err == nil {
		t.Fatal("expected the boot check to fail")
	}
	if len(b.discarded) != 0 || b.devices[id].Room != "305" {
		t.Fatal("reused entry removed or renamed", b.discarded, b.devices[id].Room)
	}
}

func TestBackendFailureDoesNotStopTheInstall(t *testing.T) {
	s := newSim(t, "register-backend-down")
	e, b := registerEngine(t, s)
	b.failWith = errors.New("无法连接管理后台")
	snap, err := e.install(registerRequest(""), hash(), func(string) {})
	if err != nil || !s.installed {
		t.Fatal("install stopped by a backend failure", err)
	}
	if snap.ServerConfigured || s.provServer != "" || !strings.Contains(snap.BackendNote, "无法连接管理后台") {
		t.Fatalf("connection state wrong after a failed registration: %+v", snap)
	}
}

func TestSessionCheckedBeforeTheTVIsTouched(t *testing.T) {
	s := newSim(t, "register-session")
	e, b := registerEngine(t, s)
	b.readyErr = errBackendSession
	_, err := e.install(registerRequest(""), hash(), func(string) {})
	if !errors.Is(err, errBackendSession) || len(s.calls) != 0 {
		t.Fatal("expired session found only after contacting the TV", err, s.calls)
	}
	e.Backend = nil
	if _, err = e.install(registerRequest(""), hash(), func(string) {}); err == nil || !strings.Contains(err.Error(), "还没有登录后台") {
		t.Fatal(err)
	}
	req := registerRequest("")
	req.Server, req.Token = "http://10.0.0.9:18790", strings.Repeat("o", 24)
	if _, err = e.install(req, hash(), func(string) {}); err == nil || len(s.calls) != 0 {
		t.Fatal("register mode with typed credentials accepted", err)
	}
	if _, err = e.install(registerRequest("a\nb"), hash(), func(string) {}); err == nil {
		t.Fatal("room name with a line break accepted")
	}
}

func TestTVThatNeverReachesTheServerIsReportedNotFailed(t *testing.T) {
	s := newSim(t, "register-offline")
	e, b := registerEngine(t, s)
	b.tvOffline = true
	var logs []string
	snap, err := e.install(registerRequest(""), hash(), func(l string) { logs = append(logs, l) })
	if err != nil || !snap.ServerConfigured || snap.ServerOnline || snap.BackendDevice == "" {
		t.Fatalf("offline TV: %v %+v", err, snap)
	}
	if !strings.Contains(strings.Join(logs, "\n"), "没看到它上线") {
		t.Fatal(logs)
	}
}

func TestArchivedEntryIsNotSilentlyRevived(t *testing.T) {
	s := newSim(t, "register-archived")
	e, b := registerEngine(t, s)
	id := backendDeviceID(digest([]byte(s.identity)))
	b.devices[id] = &fakeDevice{BackendDevice: BackendDevice{ID: id, Room: "306", Archived: true}}
	snap, err := e.install(registerRequest(""), hash(), func(string) {})
	if err != nil || snap.ServerConfigured || !strings.Contains(snap.BackendNote, "回收站") {
		t.Fatalf("%v %+v", err, snap)
	}
}

func TestBackendNamesAndRooms(t *testing.T) {
	for in, want := range map[string]string{"10.20.9.215": "http://10.20.9.215:18791", "10.20.9.215:18790": "http://10.20.9.215:18791", " 10.20.9.215:18791 ": "http://10.20.9.215:18791", "http://10.20.9.215:18790/": "http://10.20.9.215:18791", "https://hotel.example": "https://hotel.example", "hotel-srv:28791": "http://hotel-srv:28791"} {
		if got, err := backendOrigin(in); err != nil || got != want {
			t.Error(in, got, err)
		}
	}
	for _, in := range []string{"", "ftp://x", "http://u:p@10.0.0.1", "10.0.0.1/admin", "10.0.0.1:99999", "http://10.0.0.1?x=1", "-bad"} {
		if _, err := backendOrigin(in); err == nil {
			t.Error("accepted", in)
		}
	}
	if defaultRoom("192.168.4.70:5555") != "待命名-70" || defaultRoom("10.20.9.227") != "待命名-227" {
		t.Fatal(defaultRoom("192.168.4.70:5555"))
	}
	a, b := backendDeviceID("x"), backendDeviceID("y")
	if a == b || !strings.HasPrefix(a, "tv-") || len(a) != 19 || strings.Contains(a, "x") && a[3:] == digest([]byte("x"))[:16] {
		t.Fatal(a, b)
	}
	if roomProblem("301") != nil || roomProblem(strings.Repeat("房", 80)) != nil || roomProblem(strings.Repeat("房", 81)) == nil || roomProblem("a\tb") == nil {
		t.Fatal("room checks")
	}
}

// bootFailsAt makes only the at-th reboot of a run come up without the hotel client in front.
type bootFailsAt struct {
	*tvSim
	at, n int
}

func (r *bootFailsAt) Run(serial string, args ...string) (string, error) {
	if len(args) == 1 && args[0] == "reboot" {
		r.n++
		r.tvSim.rebootLosesHome = r.n == r.at
	}
	return r.tvSim.Run(serial, args...)
}

func TestOnlineConfirmationNeedsTheNewCredential(t *testing.T) {
	// A re-registered entry already has an old last_seen; a TV that cannot reach the server must not
	// be reported online even when the backend could not be read once.
	s := newSim(t, "register-baseline")
	e, b := registerEngine(t, s)
	id := backendDeviceID(digest([]byte(s.identity)))
	b.devices[id] = &fakeDevice{BackendDevice: BackendDevice{ID: id, Room: "307", Enabled: true, LastSeen: 500}, token: "old"}
	b.tvOffline = true
	snap, err := e.install(registerRequest(""), hash(), func(string) {})
	if err != nil || snap.ServerOnline || snap.BackendRoom != "307" {
		t.Fatalf("old last_seen taken for the TV coming online: %v %+v", err, snap)
	}
}

func TestEntryThatOnlyExistsIsNotTakenForTheTVsConnection(t *testing.T) {
	// The TV was registered here once, then restored back onto another server: the entry still
	// exists but the backend is not hearing from it. The TV must be moved here, not "kept".
	s := newSim(t, "register-stale-entry")
	e, b := registerEngine(t, s)
	manual := simRequest(false)
	manual.Server, manual.Token = "http://10.0.0.9:18790", strings.Repeat("o", 24)
	if _, err := e.install(manual, hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	id := backendDeviceID(digest([]byte(s.identity)))
	b.devices[id] = &fakeDevice{BackendDevice: BackendDevice{ID: id, Room: "308", Enabled: true, LastSeen: 9, IP: "192.168.4.70"}, token: "stale"}
	s.adbOpen = true
	snap, err := e.install(registerRequest(""), hash(), func(string) {})
	if err != nil || snap.BackendKept || s.provServer != b.tv || !snap.ServerOnline {
		t.Fatalf("stale entry kept the TV on its old server: %v kept=%v server=%q", err, snap.BackendKept, s.provServer)
	}
}

func TestUnreadableConnectionStateIssuesNoCredential(t *testing.T) {
	s := newSim(t, "register-status-unreadable")
	e, b := registerEngine(t, s)
	e.Runner = statusRefused{s}
	snap, err := e.install(registerRequest(""), hash(), func(string) {})
	_ = err // the later status check fails the install as before; what matters is the backend
	if b.registers != 0 || !strings.Contains(snap.BackendNote, "读不到电视现有的服务器连接状态") {
		t.Fatal("credential issued without knowing the TV's connection", b.registers, snap.BackendNote)
	}
}

type statusRefused struct{ *tvSim }

func (r statusRefused) Run(serial string, args ...string) (string, error) {
	if len(args) > 1 && strings.Contains(args[1], "provision_status") {
		return "Broadcast completed: result=0\n__HTV_RC=0", nil
	}
	return r.tvSim.Run(serial, args...)
}

func TestSessionLostWhileConfirmingStopsWaiting(t *testing.T) {
	s := newSim(t, "register-session-lost")
	e, b := registerEngine(t, s)
	b.sessionLost = true
	var logs []string
	snap, err := e.install(registerRequest(""), hash(), func(l string) { logs = append(logs, l) })
	if err != nil || snap.ServerOnline || !strings.Contains(strings.Join(logs, "\n"), "后台登录已失效，无法确认上线") {
		t.Fatal(err, snap.ServerOnline, logs)
	}
}

func TestNoteShownEvenWhenTheTVKeepsAnotherConnection(t *testing.T) {
	s := newSim(t, "register-note-configured")
	e, b := registerEngine(t, s)
	manual := simRequest(false)
	manual.Server, manual.Token = "http://10.0.0.9:18790", strings.Repeat("o", 24)
	if _, err := e.install(manual, hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	s.adbOpen = true
	b.failWith = errors.New("无法连接管理后台")
	snap, err := e.install(registerRequest(""), hash(), func(string) {})
	if err != nil || !snap.ServerConfigured || s.provServer != "http://10.0.0.9:18790" || snap.BackendNote == "" {
		t.Fatalf("%v %+v", err, snap)
	}
	res := resultFor("192.168.4.70:5555", snap, nil, false)
	if !strings.Contains(res.Message, "后台登记未成功") {
		t.Fatal("note dropped from the result:", res.Message)
	}
}

func TestEntryKeptWhenTheRestoreDidNotFullyPass(t *testing.T) {
	// The TV may still hold the credential when its restore did not pass: the entry stays.
	s := newSim(t, "register-restore-fails")
	s.rebootLosesHome, s.uninstallFails = true, true
	e, b := registerEngine(t, s)
	var logs []string
	_, err := e.install(registerRequest(""), hash(), func(l string) { logs = append(logs, l) })
	if err == nil || !strings.Contains(err.Error(), "自动恢复未完整通过") {
		t.Fatal(err)
	}
	if len(b.discarded) != 0 || len(b.devices) != 1 || !strings.Contains(strings.Join(logs, "\n"), "保留：电视的还原未完整通过") {
		t.Fatal("entry removed although the TV may still use it", b.discarded)
	}
}
