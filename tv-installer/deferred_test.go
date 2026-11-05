package main

import (
	"strings"
	"testing"
)

func offlineRequest() InstallRequest { return InstallRequest{Target: "192.168.4.62"} }

func TestDeferredInstallWritesPinWithoutConnectionAndDoesNotRollback(t *testing.T) {
	f := fake()
	e := testEngine(t, f)
	s, err := e.install(offlineRequest(), hash(), func(string) {})
	if err != nil || s.ServerConfigured || !f.installed || !f.pinConfigured || f.configured {
		t.Fatal(s, err)
	}
	sentPin := false
	for _, call := range f.calls {
		if strings.Contains(call, "staff_pin_sha256") {
			sentPin = true
		}
		if strings.Contains(call, "provision_server") || strings.Contains(call, "provision_token") || strings.Contains(call, "provision_rollback") {
			t.Fatal("wrote/rolled back connection during deferred install", call)
		}
	}
	if !sentPin {
		t.Fatal("missing PIN-only configuration")
	}
}
func TestDeferredResultIsPendingServerInsteadOfFailedOrPlaybackReady(t *testing.T) {
	f := fake()
	a := &App{Engine: testEngine(t, f), job: &Job{ID: strings.Repeat("a", 32), State: "running"}}
	a.op.Lock()
	a.runInstall([]InstallRequest{offlineRequest()}, hash())
	if len(a.job.Results) != 1 {
		t.Fatal(a.job)
	}
	result := a.job.Results[0]
	if result.State != "pending_server" || result.ConnectionState != "pending" || !strings.Contains(result.Message, "服务器可稍后") {
		t.Fatal(result)
	}
	if a.job.State != "finished" {
		t.Fatal(a.job.State)
	}
}
func TestConnectionCanBeAddedAfterDeferredInstallWithoutReplacingPin(t *testing.T) {
	f := fake()
	e := testEngine(t, f)
	if _, err := e.install(offlineRequest(), hash(), func(string) {}); err != nil {
		t.Fatal(err)
	}
	f.calls = nil
	s, err := e.install(request(), "", func(string) {})
	if err != nil || !s.ServerConfigured || !f.pinConfigured {
		t.Fatal(s, err)
	}
	for _, call := range f.calls {
		if strings.Contains(call, "staff_pin_sha256") {
			t.Fatal("unexpected PIN change", call)
		}
	}
}
func TestDeferredUpgradePreservesExistingConnection(t *testing.T) {
	f := fake()
	f.installed = true
	f.configured = true
	f.pinConfigured = true
	e := testEngine(t, f)
	s, err := e.install(offlineRequest(), "", func(string) {})
	if err != nil || !s.ServerConfigured || !f.pinConfigured {
		t.Fatal(s, err)
	}
	for _, call := range f.calls {
		if strings.Contains(call, "provision_server") || strings.Contains(call, "provision_token") {
			t.Fatal("changed existing connection", call)
		}
	}
}
func TestDeferredInstallStillRequiresConfirmedPin(t *testing.T) {
	f := fake()
	f.missingConfig = true
	e := testEngine(t, f)
	if _, err := e.install(offlineRequest(), hash(), func(string) {}); err == nil || !strings.Contains(err.Error(), "员工PIN") {
		t.Fatal(err)
	}
	if f.focus != "com.vendor/.Home" {
		t.Fatal("did not restore home after bad PIN state")
	}
}
func TestOptionalConnectionStillRejectsExplicitMalformedValuesBeforeTVCommands(t *testing.T) {
	for _, req := range []InstallRequest{
		{Target: "192.168.4.62", Server: "not-a-url", Token: "test-device-token-1234"},
		{Target: "192.168.4.62", Server: "http://192.168.4.185:18790"},
		{Target: "192.168.4.62", Token: "test-device-token-1234"},
	} {
		f := fake()
		e := testEngine(t, f)
		if _, err := e.install(req, hash(), func(string) {}); err == nil || len(f.calls) != 0 {
			t.Fatal("accepted partial or malformed explicit connection", req)
		}
	}
}
func TestStatusSeparatesUnconfiguredServerFromMissingPinAndBadReply(t *testing.T) {
	for _, row := range []struct {
		raw                string
		server, pin, valid bool
	}{
		{"Broadcast completed: result=200, data=hotel_configured=false pin_configured=true", false, true, true},
		{"Broadcast completed: result=200, data=hotel_configured=true pin_configured=false", true, false, true},
		{"Broadcast completed: result=200, data=pin_configured=true", false, false, false},
		{"Broadcast completed: result=0, data=hotel_configured=false pin_configured=true", false, false, false},
	} {
		server, pin, err := provisioningState(row.raw)
		if (err == nil) != row.valid || server != row.server || pin != row.pin {
			t.Fatal(row, server, pin, err)
		}
	}
}

func TestDeferredBatchAcceptsOnlyIPsAndPinForBothDevices(t *testing.T) {
	f := fake()
	a := &App{Engine: testEngine(t, f), job: &Job{ID: strings.Repeat("b", 32), State: "running"}}
	a.op.Lock()
	a.runInstall([]InstallRequest{{Target: "192.168.4.62"}, {Target: "192.168.4.63"}}, hash())
	if len(a.job.Results) != 2 {
		t.Fatal(a.job)
	}
	for _, r := range a.job.Results {
		if r.State != "pending_server" || r.ConnectionState != "pending" {
			t.Fatal(r)
		}
	}
}
