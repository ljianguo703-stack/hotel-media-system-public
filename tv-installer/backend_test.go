package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeAdmin speaks the admin API of server.py (admin_http.py) closely enough for the client's error
// handling; the real server is exercised by TestBackendAgainstRealServer.
type fakeAdmin struct {
	mu          sync.Mutex
	role        string
	noCookie    bool
	conflicts   int // devices POSTs answered 409 before succeeding
	revision    int
	posts       []string
	logouts     int
	session     bool
	password    string
	devices     []map[string]any
	opConflicts int // operations POSTs answered 409 before succeeding
	ops         []string
	block       chan struct{} // when set, state requests wait on it
}

func (f *fakeAdmin) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	send := func(code int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		json.NewEncoder(w).Encode(v)
	}
	if r.Method == "POST" {
		f.posts = append(f.posts, r.URL.Path)
	}
	switch {
	case r.URL.Path == "/api/admin/login":
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		if body["password"] != f.password {
			send(401, map[string]string{"error": "账号或密码错误"})
			return
		}
		if !f.noCookie {
			http.SetCookie(w, &http.Cookie{Name: "hotel_session", Value: "sess-1", HttpOnly: true})
		}
		f.session = true
		send(200, map[string]any{"user": map[string]string{"name": body["username"], "role": f.role, "csrf": "csrf-1"}})
		return
	case r.URL.Path == "/api/admin/session":
		if !f.session {
			send(200, map[string]any{"user": nil})
			return
		}
		send(200, map[string]any{"user": map[string]string{"role": f.role}})
		return
	}
	if c, err := r.Cookie("hotel_session"); err != nil || c.Value != "sess-1" || !f.session {
		send(401, map[string]string{"error": "请先登录后台"})
		return
	}
	if r.Method == "POST" && r.Header.Get("X-CSRF-Token") != "csrf-1" {
		send(403, map[string]string{"error": "操作校验失效，请重新登录"})
		return
	}
	switch {
	case r.URL.Path == "/api/admin/logout":
		f.session = false
		f.logouts++
		send(200, map[string]any{})
	case r.URL.Path == "/api/admin/state":
		if f.block != nil {
			f.mu.Unlock()
			<-f.block
			f.mu.Lock()
		}
		devices := f.devices
		if devices == nil {
			devices = []map[string]any{}
		}
		send(200, map[string]any{"endpoints": map[string]int{"tv_port": 18790}, "revision": f.revision, "settings": map[string]string{"hotel": "测试酒店"}, "devices": devices})
	case r.URL.Path == "/api/admin/operations" && r.Method == "POST":
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if f.opConflicts > 0 || body["revision"] != float64(f.revision) {
			if f.opConflicts > 0 {
				f.opConflicts--
			}
			f.revision++
			send(409, map[string]string{"error": "数据已被其他操作更新，请刷新后重试"})
			return
		}
		op, _ := body["operation"].(string)
		f.ops = append(f.ops, body["action"].(string)+":"+op)
		send(200, map[string]any{"ok": body["ids"], "failed": []any{}, "credentials": []any{}})
	case strings.HasPrefix(r.URL.Path, "/api/admin/devices/"):
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if f.conflicts > 0 || body["revision"] != float64(f.revision) {
			if f.conflicts > 0 {
				f.conflicts--
			}
			f.revision++
			send(409, map[string]string{"error": "数据已被其他操作更新，请刷新后重试"})
			return
		}
		send(200, map[string]any{"id": strings.TrimPrefix(r.URL.Path, "/api/admin/devices/"), "token": strings.Repeat("t", 43)})
	default:
		send(404, map[string]string{"error": "接口不存在"})
	}
}

func adminServer(t *testing.T, f *fakeAdmin) string {
	s := httptest.NewServer(f)
	t.Cleanup(s.Close)
	return strings.TrimPrefix(s.URL, "http://")
}

func TestBackendLoginKeepsNoPasswordAndNeedsAnAdmin(t *testing.T) {
	f := &fakeAdmin{role: "operator", password: "pw-123456"}
	addr := adminServer(t, f)
	b := &Backend{}
	if _, err := b.Login(addr, "staff", "pw-123456"); err == nil || !strings.Contains(err.Error(), "需要管理员") {
		t.Fatal("non-admin account accepted", err)
	}
	if b.Status().LoggedIn || f.logouts != 1 {
		t.Fatal("non-admin session kept open", f.logouts)
	}
	f.role = "admin"
	st, err := b.Login(addr, "admin", "pw-123456")
	if err != nil || !st.LoggedIn || st.Hotel != "测试酒店" || !strings.HasSuffix(st.TV, ":18790") {
		t.Fatal(st, err)
	}
	raw, _ := json.Marshal(st)
	if strings.Contains(string(raw), "pw-123456") || strings.Contains(string(raw), "sess-1") || strings.Contains(string(raw), "csrf-1") {
		t.Fatal("status exposes a secret", string(raw))
	}
}

func TestBackendRetriesOnceWhenTheRevisionMoved(t *testing.T) {
	f := &fakeAdmin{role: "admin", password: "p", conflicts: 1}
	b := &Backend{}
	if _, err := b.Login(adminServer(t, f), "admin", "p"); err != nil {
		t.Fatal(err)
	}
	if tok, _, created, err := b.Register("tv-1", "待命名-1", "m"); err != nil || !created || tok == "" {
		t.Fatal("409 not retried with the new revision", err)
	}
	f.conflicts = 2
	if _, _, _, err := b.Register("tv-2", "x", "m"); err == nil || !strings.Contains(err.Error(), "数据已被其他操作更新") {
		t.Fatal("endless retries or swallowed conflict", err)
	}
}

func TestBackendErrorsSayWhatToDo(t *testing.T) {
	f := &fakeAdmin{role: "admin", password: "p"}
	addr := adminServer(t, f)
	b := &Backend{}
	if _, err := b.Login(addr, "admin", "wrong"); err == nil || err.Error() != "后台返回：账号或密码错误" {
		t.Fatal(err)
	}
	if _, err := b.Login(addr, "", "p"); err == nil {
		t.Fatal("empty account")
	}
	f.noCookie = true
	if _, err := b.Login(addr, "admin", "p"); err == nil || b.Status().LoggedIn {
		t.Fatal("login without a session cookie accepted", err)
	}
	f.noCookie = false
	if _, err := b.Login(addr, "admin", "p"); err != nil {
		t.Fatal(err)
	}
	f.session = false // logged out in the backend, or 12 hours passed
	if _, _, _, err := b.Register("tv-1", "x", "m"); err != errBackendSession || b.Status().LoggedIn {
		t.Fatal("ended session found by a registration not reported or still shown as logged in", err)
	}
	f.session = true
	b.cookie, b.csrf = "sess-1", "csrf-1"
	f.session = false
	if err := b.Ready(); err != errBackendSession {
		t.Fatal(err)
	}
	if b.Status().LoggedIn {
		t.Fatal("dead session still shown as logged in")
	}
	// Nothing listening: the message names the admin port.
	if _, err := (&Backend{}).Login("127.0.0.1:1", "admin", "p"); err == nil || !strings.Contains(err.Error(), "18791") {
		t.Fatal(err)
	}
	// The TV service port answers 404 on admin paths.
	tvPort := httptest.NewServer(http.NotFoundHandler())
	defer tvPort.Close()
	if _, err := (&Backend{}).Login(strings.TrimPrefix(tvPort.URL, "http://"), "admin", "p"); err == nil || !strings.Contains(err.Error(), "不是管理后台") {
		t.Fatal(err)
	}
}

func TestAPIBackendLoginAndRegisterValidation(t *testing.T) {
	a := newTestApp(t)
	f := &fakeAdmin{role: "admin", password: "pw-xyz-123"}
	addr := adminServer(t, f)
	w := call(a, "/api/install", `{"devices":[{"target":"192.168.4.70","register":true}]}`, a.Token, "", a.Host)
	if w.Code != 400 || !strings.Contains(w.Body.String(), "还没有登录后台") {
		t.Fatal(w.Code, w.Body.String())
	}
	for _, local := range []string{addr, "localhost", "0.0.0.0:18791", "http://LOCALHOST:18791"} {
		w = call(a, "/api/backend-login", `{"server":"`+local+`","username":"admin","password":"pw-xyz-123"}`, a.Token, "", a.Host)
		if w.Code != 400 || !strings.Contains(w.Body.String(), "不能用 127.0.0.1") || f.session {
			t.Fatal("loopback server accepted:", local, w.Body.String())
		}
	}
	a.allowLoopbackBackend = true // the fake admin API listens on 127.0.0.1
	w = call(a, "/api/backend-login", `{"server":"`+addr+`","username":"admin","password":"pw-xyz-123"}`, a.Token, "", a.Host)
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"logged_in":true`) {
		t.Fatal(w.Body.String())
	}
	w = call(a, "/api/state", "", a.Token, "", a.Host)
	if !strings.Contains(w.Body.String(), `"logged_in":true`) || strings.Contains(w.Body.String(), "pw-xyz-123") || strings.Contains(w.Body.String(), "sess-1") {
		t.Fatal("state", w.Body.String())
	}
	for body, want := range map[string]string{
		`{"devices":[{"target":"192.168.4.70","register":true,"server":"http://10.0.0.1:18790","token":"aaaaaaaaaaaaaaaaaaaa"}]}`: "不用填写服务器地址和设备凭证",
		`{"devices":[{"target":"192.168.4.70","register":true,"room":"` + strings.Repeat("房", 81) + `"}]}`:                        "房间名最多",
		`{"devices":[{"target":"192.168.4.70","register":true},{"target":"192.168.4.71","register":true,"room":"a\nb"}]}`:         "第 2 台",
	} {
		if w = call(a, "/api/install", body, a.Token, "", a.Host); w.Code != 400 || !strings.Contains(w.Body.String(), want) {
			t.Fatal(body, w.Code, w.Body.String())
		}
	}
	// Nothing about the backend is written to the tool's data folder.
	filepath.Walk(a.Engine.State, func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			if b, _ := os.ReadFile(p); strings.Contains(string(b), "pw-xyz-123") || strings.Contains(string(b), "sess-1") || strings.Contains(string(b), "csrf-1") {
				t.Fatal("secret on disk:", p)
			}
		}
		return nil
	})
	w = call(a, "/api/backend-logout", "{}", a.Token, "", a.Host)
	if w.Code != 200 || strings.Contains(w.Body.String(), `"logged_in":true`) || f.logouts != 1 {
		t.Fatal(w.Body.String(), f.logouts)
	}
}

func TestLookupOnlyTakesAnEntryTheBackendHearsFromAtThisAddress(t *testing.T) {
	f := &fakeAdmin{role: "admin", password: "p"}
	b := &Backend{}
	if _, err := b.Login(adminServer(t, f), "admin", "p"); err != nil {
		t.Fatal(err)
	}
	dev := func(id string, online bool, ip string, seen int, archived, enabled bool) map[string]any {
		return map[string]any{"id": id, "room": id, "online": online, "ip": ip, "last_seen": seen, "_archived": archived, "enabled": enabled}
	}
	for _, tc := range []struct {
		name    string
		devices []map[string]any
		want    string
	}{
		{"own entry, heard from here", []map[string]any{dev("tv-own", true, "10.0.0.5", 5, false, true)}, "tv-own"},
		{"own entry, not heard from", []map[string]any{dev("tv-own", false, "10.0.0.5", 5, false, true)}, ""},
		{"own entry, heard from elsewhere", []map[string]any{dev("tv-own", true, "10.0.0.9", 5, false, true)}, ""},
		{"own entry archived", []map[string]any{dev("tv-own", true, "10.0.0.5", 5, true, false)}, ""},
		{"own entry disabled", []map[string]any{dev("tv-own", true, "10.0.0.5", 5, false, false)}, ""},
		{"hand-made entry online here", []map[string]any{dev("room-301", true, "10.0.0.5", 5, false, true)}, "room-301"},
		{"hand-made entry offline", []map[string]any{dev("room-301", false, "10.0.0.5", 5, false, true)}, ""},
		{"newest of two at this address", []map[string]any{dev("old", true, "10.0.0.5", 3, false, true), dev("new", true, "10.0.0.5", 9, false, true), dev("older", true, "10.0.0.5", 1, false, true)}, "new"},
	} {
		f.devices = tc.devices
		d, found, err := b.Lookup("tv-own", "10.0.0.5")
		if err != nil || found != (tc.want != "") || (found && d.ID != tc.want) {
			t.Errorf("%s: got %q found=%v err=%v", tc.name, d.ID, found, err)
		}
	}
}

func TestDiscardEmptiesTheEntryAndRetriesOnConflict(t *testing.T) {
	f := &fakeAdmin{role: "admin", password: "p", opConflicts: 1}
	b := &Backend{}
	if _, err := b.Login(adminServer(t, f), "admin", "p"); err != nil {
		t.Fatal(err)
	}
	if err := b.Discard("tv-x"); err != nil {
		t.Fatal(err)
	}
	if strings.Join(f.ops, ",") != "devices_batch:archive,purge:" {
		t.Fatal("discard did not archive then clear:", f.ops)
	}
}

func TestWindowStateDoesNotWaitForTheBackend(t *testing.T) {
	// Review round 6, finding 7: a slow backend request must not freeze /api/state.
	f := &fakeAdmin{role: "admin", password: "p"}
	b := &Backend{}
	if _, err := b.Login(adminServer(t, f), "admin", "p"); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	f.block = make(chan struct{})
	f.mu.Unlock()
	release := sync.OnceFunc(func() { close(f.block) })
	defer release() // a failure below must not leave the server's handler blocked
	done := make(chan struct{})
	go func() { b.Lookup("tv-x", "10.0.0.5"); close(done) }()
	time.Sleep(300 * time.Millisecond) // let the request reach the server and wait there
	got := make(chan BackendStatus, 1)
	go func() { got <- b.Status() }()
	select {
	case st := <-got:
		if !st.LoggedIn {
			t.Fatal(st)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Status waited for a backend request in progress")
	}
	release()
	<-done
}

func TestBackendLogoutRefusedWhileInstalling(t *testing.T) {
	a := newTestApp(t)
	a.job = &Job{ID: "j", State: "running"}
	for _, path := range []string{"/api/backend-logout", "/api/backend-login"} {
		body := "{}"
		if path == "/api/backend-login" {
			body = `{"server":"10.0.0.1","username":"a","password":"b"}`
		}
		if w := call(a, path, body, a.Token, "", a.Host); w.Code != 400 || !strings.Contains(w.Body.String(), "安装任务进行中") {
			t.Fatal(path, w.Code, w.Body.String())
		}
	}
}
