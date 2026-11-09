package main

import (
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type connectionRunner struct {
	output string
	err    error
	state  string
	calls  []string
}

func (r *connectionRunner) Run(serial string, args ...string) (string, error) {
	r.calls = append(r.calls, serial+"|"+strings.Join(args, "|"))
	switch args[0] {
	case "disconnect":
		return "disconnected", nil
	case "connect":
		return r.output, r.err
	case "get-state":
		if r.state == "device" {
			return r.state, nil
		}
		return r.state, errors.New("exit status 1")
	}
	return "", errors.New("unexpected operation")
}
func TestConnectClassifiesReplyInsteadOfAssumingAuthorization(t *testing.T) {
	cases := []struct {
		name, out, state, code, detail string
		commandError                   bool
	}{
		{"zero exit refusal", "failed to connect to '192.168.4.62:5555': Connection refused", "", "network", "Connection refused", false},
		{"unreachable route", "cannot connect to 192.168.4.62:5555: No route to host", "", "network", "No route to host", false},
		{"waiting for trust", "failed to authenticate to 192.168.4.62:5555", "error: device not found", "unauthorized", "failed to authenticate", false},
		{"state unauthorized", "connected to 192.168.4.62:5555", "error: device unauthorized", "unauthorized", "device unauthorized", false},
		{"offline", "already connected to 192.168.4.62:5555", "offline", "offline", "offline", false},
		{"missing transport", "connected to 192.168.4.62:5555", "error: device not found", "transport", "device not found", false},
		{"executable error", "", "", "adb", "exec denied", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := &connectionRunner{output: tc.out, state: tc.state}
			if tc.commandError {
				r.err = errors.New("exec denied")
			}
			err := connect(r, "192.168.4.62:5555")
			var got *ADBConnectionError
			if !errors.As(err, &got) || got.Code != tc.code || !strings.Contains(got.Detail, tc.detail) {
				t.Fatalf("wrong connection result: %#v, %v", got, err)
			}
			if tc.code == "network" && len(r.calls) != 1 {
				t.Fatal("network failure was polled as consent", r.calls)
			}
		})
	}
}
func TestReconnectOnlyTouchesSelectedTarget(t *testing.T) {
	r := &connectionRunner{output: "connected", state: "device"}
	if err := reconnectTV(r, "192.168.4.62:5555"); err != nil {
		t.Fatal(err)
	}
	want := "|disconnect|192.168.4.62:5555\n|connect|192.168.4.62:5555\n192.168.4.62:5555|get-state"
	if strings.Join(r.calls, "\n") != want {
		t.Fatal(r.calls)
	}
}
func TestReconnectAPIKeepsSessionTargetAndBusyGuards(t *testing.T) {
	for _, kind := range []string{"session", "target", "busy", "method", "success", "network"} {
		t.Run(kind, func(t *testing.T) {
			a := newTestApp(t)
			r := &connectionRunner{output: "connected", state: "device"}
			a.Engine.Runner = r
			body := `{"target":"192.168.4.62"}`
			token := a.Token
			switch kind {
			case "session":
				token = ""
			case "target":
				body = `{"target":"8.8.8.8:5555"}`
			case "busy":
				a.job = &Job{State: "running"}
			case "method":
				body = ""
			case "network":
				r.output = "failed to connect: Connection refused"
			}
			w := call(a, "/api/reconnect", body, token, "", a.Host)
			if kind == "success" {
				if w.Code != 200 || len(r.calls) != 3 || a.job != nil {
					t.Fatal(w.Code, w.Body.String(), r.calls)
				}
				return
			}
			if w.Code < 400 {
				t.Fatal("guard accepted", kind, w.Code)
			}
			if kind == "network" {
				var out struct {
					Connection *ADBConnectionError `json:"connection"`
				}
				if json.Unmarshal(w.Body.Bytes(), &out) != nil || out.Connection == nil || out.Connection.Code != "network" {
					t.Fatal(w.Body.String())
				}
				return
			}
			if len(r.calls) != 0 {
				t.Fatal("guard contacted a device", r.calls)
			}
		})
	}
}
func TestImportTVPointOnSecondComputerOnlyReadsTV(t *testing.T) {
	tv := newSim(t, "second-computer-api")
	first := simEngine(t, tv, true)
	if _, err := first.install(simRequest(false), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	tv.adbOpen = true
	second := simEngine(t, tv, true)
	a := &App{Engine: second, Token: "test-local-session-secret", Host: "127.0.0.1:18880"}
	before := len(tv.calls)
	installed, boot, home := tv.installed, tv.bootID, tv.preferred
	w := call(a, "/api/restore-from-tv", `{"target":"192.168.4.70"}`, a.Token, "", a.Host)
	var point Snapshot
	if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &point) != nil || !point.FromTV || point.ID == "" {
		t.Fatal(w.Code, w.Body.String())
	}
	if a.job != nil || tv.installed != installed || tv.bootID != boot || tv.preferred != home {
		t.Fatal("reading point performed a restore")
	}
	if _, err := os.Stat(filepath.Join(second.State, "backups", point.ID, "snapshot.json")); err != nil {
		t.Fatal(err)
	}
	for _, line := range tv.calls[before:] {
		for _, op := range []string{"|install|", "|reboot", "|push|", "'settings' 'put'", "'pm' 'uninstall'"} {
			if strings.Contains(line, op) {
				t.Fatal("reading point changed TV state", line)
			}
		}
	}
}
func TestNativeADBZeroExitRefusalIsNetworkFailure(t *testing.T) {
	path := os.Getenv("TI_NATIVE_ADB")
	if path == "" {
		t.Skip("native ADB integration opt-in")
	}
	t.Setenv("ANDROID_USER_HOME", t.TempDir())
	free := func() string {
		ln, err := net.Listen("tcp4", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		address := ln.Addr().String()
		ln.Close()
		return address
	}
	_, port, _ := net.SplitHostPort(free())
	target := free()
	a := ADB{Path: path, Port: port}
	defer a.Run("", "kill-server")
	err := connect(a, target)
	var result *ADBConnectionError
	if !errors.As(err, &result) || result.Code != "network" || !strings.Contains(result.Detail, "Connection refused") {
		t.Fatal(err)
	}
}
