package main

import (
	"encoding/json"
	"errors"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type packageQueryFixture struct {
	path, list       string
	pathErr, listErr error
	calls            []string
}

func (f *packageQueryFixture) Run(serial string, args ...string) (string, error) {
	command := strings.Join(args, " ")
	f.calls = append(f.calls, command)
	if strings.Contains(command, "'pm' 'path'") {
		return rcWrap(args, f.path, f.pathErr)
	}
	if strings.Contains(command, "'pm' 'list' 'packages' '--user' '0'") {
		if f.listErr != nil && strings.Contains(f.listErr.Error(), "offline") {
			return "", f.listErr
		}
		return rcWrap(args, f.list, f.listErr)
	}
	return "", errors.New("unexpected command")
}
func TestMissingClientExitOneIsFreshInstall(t *testing.T) {
	f := &packageQueryFixture{pathErr: errors.New("exit status 1"), list: "package:android\npackage:com.vendor.home"}
	paths, err := clientPaths(f, "192.168.4.62:5555")
	if err != nil || len(paths) != 0 || len(f.calls) != 2 {
		t.Fatal(paths, err, f.calls)
	}
}
func TestMissingClientRequiresValidIndependentPackageInventory(t *testing.T) {
	for _, row := range []struct {
		name, list string
		err        error
	}{
		{"empty", "", nil}, {"malformed", "Error: package manager unavailable", nil},
		{"transport", "", errors.New("device offline")}, {"registered", "package:android\npackage:" + pkg, nil},
		{"not_exact_match", "package:android\npackage:" + pkg + ".another", nil},
	} {
		t.Run(row.name, func(t *testing.T) {
			f := &packageQueryFixture{pathErr: errors.New("exit status 1"), list: row.list, listErr: row.err}
			paths, err := clientPaths(f, "192.168.4.62:5555")
			if row.name == "not_exact_match" {
				if err != nil || len(paths) != 0 {
					t.Fatal(paths, err)
				}
			} else if err == nil {
				t.Fatal("treated broken/registered query as absence")
			}
		})
	}
}
func TestClientPathRejectsDeniedOrMalformedResponse(t *testing.T) {
	for _, row := range []struct {
		name, path string
		err        error
	}{
		{"permission", "java.lang.SecurityException: denied", errors.New("exit status 1")},
		{"printed_failure_exit_zero", "Error: package manager died", nil},
		{"relative", "package:data/app/base.apk", nil}, {"not_apk", "package:/data/app/base.zip", nil},
		{"partial", "package:/data/app/base.apk\nException occurred", nil},
		{"duplicate", "package:/data/app/base.apk\npackage:/data/app/base.apk", nil},
		{"path_but_failed", "package:/data/app/base.apk", errors.New("exit status 1")},
	} {
		t.Run(row.name, func(t *testing.T) {
			f := &packageQueryFixture{path: row.path, pathErr: row.err, list: "package:android"}
			if _, err := clientPaths(f, "192.168.4.62:5555"); err == nil {
				t.Fatal("accepted unsafe response")
			}
			if len(f.calls) != 1 {
				t.Fatal("masked explicit failure with package listing")
			}
		})
	}
}
func TestClientSplitAPKPathsKeptForBackup(t *testing.T) {
	f := &packageQueryFixture{path: "package:/data/app/base.apk\npackage:/data/app/split_config.arm64_v8a.apk\n"}
	paths, err := clientPaths(f, "192.168.4.62:5555")
	if err != nil || len(paths) != 2 || len(f.calls) != 1 {
		t.Fatal(paths, err)
	}
}
func TestFreshInstallMissingPinStopsBeforeBackup(t *testing.T) {
	for _, row := range []struct {
		request InstallRequest
		pin     string
	}{{InstallRequest{Target: "192.168.4.62"}, ""}, {request(), ""}} {
		f := fake()
		e := testEngine(t, f)
		snap, err := e.install(row.request, row.pin, func(string) {})
		if err == nil || !strings.Contains(err.Error(), "首次安装") || snap.ID != "" {
			t.Fatal(snap, err)
		}
		if _, err = os.Stat(filepath.Join(e.State, "backups")); !os.IsNotExist(err) {
			t.Fatal("created backup before first-install requirements")
		}
		for _, call := range f.calls {
			if strings.Contains(call, "'settings'") || strings.Contains(call, " install ") {
				t.Fatal("changed/read unrelated settings before configuration", call)
			}
		}
	}
}
func TestProbeReportsFreshInstallAndUpgrade(t *testing.T) {
	f := fake()
	a := &App{Engine: testEngine(t, f), Host: "127.0.0.1:12345", Token: "session"}
	for _, installed := range []bool{false, true} {
		f.installed = installed
		r := httptest.NewRequest("POST", "http://"+a.Host+"/api/probe", strings.NewReader(`{"target":"192.168.4.62"}`))
		r.Header.Set("X-App-Token", a.Token)
		w := httptest.NewRecorder()
		a.handler().ServeHTTP(w, r)
		var reply struct {
			Installed *bool `json:"client_installed"`
		}
		if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &reply) != nil || reply.Installed == nil || *reply.Installed != installed {
			t.Fatal(w.Code, w.Body.String())
		}
	}
}
