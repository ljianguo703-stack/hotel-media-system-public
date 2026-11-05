package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ---- 1.3.3: field-specific connection checks (老板的 10.20.9.215:18791 报错) ----

func TestServerAddressWithoutSchemeIsCompleted(t *testing.T) {
	for raw, want := range map[string]string{
		"10.20.9.215:18791":        "http://10.20.9.215:18791",
		" 10.20.9.215:18790/ ":     "http://10.20.9.215:18790/",
		"10.20.9.215":              "http://10.20.9.215",
		"tv-server:18790":          "http://tv-server:18790",
		"hotel.lan":                "http://hotel.lan",
		"http://10.20.9.215:18790": "http://10.20.9.215:18790",
		"https://tv.example.com":   "https://tv.example.com",
		"not-a-url":                "not-a-url", // no dot, no port: left as typed, reported
		"10.20.9.215:18790/api":    "10.20.9.215:18790/api",
		"ftp://10.20.9.215":        "ftp://10.20.9.215",
		"":                         "",
	} {
		if got := normalizeServer(raw); got != want {
			t.Fatalf("%q → %q, want %q", raw, got, want)
		}
	}
}

func TestConnectionProblemNamesTheWrongField(t *testing.T) {
	good := "test-device-token-1234"
	for _, c := range []struct{ server, token, want string }{
		{"", "", ""},
		{"http://10.20.9.215:18790", good, ""},
		{"", good, "没有服务器地址"},
		{"http://10.20.9.215:18790", "", "没有设备凭证"},
		{"not-a-url", good, "服务器地址格式不对"},
		{"http://10.20.9.215:18790/admin", good, "服务器地址格式不对"},
		{"http://10.20.9.215:18790", "short", "设备凭证格式不对"},
		{"http://10.20.9.215:18790", "带中文的凭证-1234567890", "设备凭证格式不对"},
		{"http://10.20.9.215:18790", "has space-1234567890", "设备凭证格式不对"},
	} {
		err := connectionProblem(c.server, c.token)
		if (c.want == "") != (err == nil) || (err != nil && !strings.Contains(err.Error(), c.want)) {
			t.Fatalf("%q/%q: %v (want %q)", c.server, c.token, err, c.want)
		}
	}
	if adminPortHint("http://10.20.9.215:18791") == "" || adminPortHint("http://10.20.9.215:18790") != "" {
		t.Fatal("admin port hint")
	}
}

func TestBareServerAddressIsProvisionedWithScheme(t *testing.T) {
	s := newSim(t, "bare-server-address")
	e := simEngine(t, s, true)
	req := simRequest(false)
	req.Server, req.Token = "10.20.9.215:18791", "test-device-token-1234"
	var logs []string
	if _, err := e.install(req, hash(), func(l string) { logs = append(logs, l) }); err != nil {
		t.Fatal(err)
	}
	if !s.called("'provision_server' 'http://10.20.9.215:18791'") {
		t.Fatal("address not completed with http:// before it was written to the TV")
	}
	if !strings.Contains(strings.Join(logs, "\n"), "18791 通常是管理后台") {
		t.Fatal("admin port not pointed out")
	}
}

func TestInstallAPIRejectsBadConnectionBeforeTheJobStarts(t *testing.T) {
	a := newTestApp(t)
	w := call(a, "/api/install", `{"devices":[{"target":"10.20.9.227","server":"10.20.9.215:18791","token":"bad token"}]}`, a.Token, "", a.Host)
	if w.Code != 400 || !strings.Contains(w.Body.String(), "设备凭证格式不对") || a.job != nil {
		t.Fatal(w.Code, w.Body.String())
	}
	w = call(a, "/api/install", `{"devices":[{"target":"10.20.9.227","server":"http://10.20.9.215:18790","token":"good-device-token-0001"},{"target":"10.20.9.228","server":"http://10.20.9.215:18790","token":"x"}]}`, a.Token, "", a.Host)
	if w.Code != 400 || !strings.Contains(w.Body.String(), "第 2 台") || a.job != nil {
		t.Fatal(w.Code, w.Body.String())
	}
	w = call(a, "/api/server-check", `{"server":"10.20.9.215:18790","token":"short"}`, a.Token, "", a.Host)
	if w.Code != 400 || !strings.Contains(w.Body.String(), "设备凭证格式不对") {
		t.Fatal(w.Code, w.Body.String())
	}
}

func TestRestoredJobIsMarkedAsAnEarlierRun(t *testing.T) {
	// 老板: after quitting and restarting, the old error still showed, as if it had failed again.
	state := t.TempDir()
	id := strings.Repeat("a", 32)
	os.MkdirAll(filepath.Join(state, "reports"), 0700)
	atomicJSON(filepath.Join(state, "reports", id+".json"), Job{ID: id, State: "done", Lines: []string{}, Results: []Result{{Target: "10.20.9.227:5555", State: "failed", Message: "服务器地址或设备凭证格式无效"}}})
	j := latestJob(state)
	if j == nil || !j.Restored || j.At == "" {
		t.Fatal("restored job not marked or without a time", j)
	}
}

func TestFailureBeforeContactingTheTVSaysNothingChanged(t *testing.T) {
	a := newTestApp(t)
	a.job = &Job{ID: strings.Repeat("b", 32), State: "running", Lines: []string{}, Results: []Result{}}
	a.op.Lock()
	a.runInstall([]InstallRequest{{Target: "192.168.4.62:5555"}}, "not-a-valid-pin-hash")
	if len(a.job.Results) != 1 || !strings.Contains(a.job.Results[0].Message, "未连接电视，没有改动") {
		t.Fatal(a.job.Results)
	}
}
