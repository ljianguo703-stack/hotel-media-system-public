package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestManagedEnrollmentRequiresIndividualOnlineRebootAndRoom(t *testing.T) {
	for _, req := range []InstallRequest{
		{Managed: true, Room: "101"},
		{Managed: true, Register: true, Room: "101", CloseADB: true},
		{Managed: true, Register: true, Room: "101", SkipRebootCheck: true},
		{Managed: true, Register: true},
	} {
		// A nil runner would panic if an invalid request contacted a TV.
		if _, err := (Engine{}).install(req, "", func(string) {}); err == nil {
			t.Fatal("accepted incomplete fleet onboarding", req)
		}
	}
}

func TestManagedResultCannotClaimReadyWithoutServerProbe(t *testing.T) {
	s := Snapshot{ManagedRequested: true, ServerConfigured: true, ServerOnline: true, RebootVerified: true, BackendRoom: "101", BackendNote: "等待服务器密钥授权"}
	r := resultFor("192.168.1.20:5555", s, nil, false)
	if r.State != "pending_management" || r.ManagedReady {
		t.Fatal("pending authorization reported as managed", r)
	}
	s.ManagedReady = true
	s.BackendNote = ""
	r = resultFor("192.168.1.20:5555", s, nil, false)
	if !r.ManagedReady || r.State == "pending_management" {
		t.Fatal("verified enrollment missing", r)
	}
}

func TestEnrollmentBackendHandsOffIdentityAndWaitsForActualWorker(t *testing.T) {
	var captured map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/admin/software":
			json.NewEncoder(w).Encode(map[string]any{"revision": 5, "jobs": []any{map[string]any{"id": "sw-fixture", "status": "completed", "steps": []any{map[string]any{"message": "verified"}}}}})
		case "/api/admin/fleet/enroll":
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Error(err)
			}
			json.NewEncoder(w).Encode(map[string]any{"job": map[string]any{"id": "sw-fixture"}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	b := &Backend{admin: server.URL, cookie: "fixture-only", csrf: "fixture-only", client: server.Client()}
	if err := b.EnrollManaged("tv-fixture", "192.168.1.20:5555", strings.Repeat("a", 64), func(string) {}); err != nil {
		t.Fatal(err)
	}
	if captured["id"] != "tv-fixture" || captured["address"] != "192.168.1.20" || captured["port"] != float64(5555) || captured["identity_sha256"] != strings.Repeat("a", 64) || captured["reboot_verified"] != true || captured["online_confirmed"] != true {
		t.Fatal("incomplete enrollment handoff", captured)
	}
}
