package main

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

func TestADBSessionWorkerHelper(t *testing.T) {
	if os.Getenv("TI_ADB_WORKER_HELPER") != "1" {
		return
	}
	if err := runADBWorker(os.Getenv("TI_NATIVE_ADB")); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	os.Exit(0)
}

func nativeSession(t *testing.T) *adbSession {
	t.Helper()
	path := os.Getenv("TI_NATIVE_ADB")
	if path == "" {
		t.Skip("native ADB integration opt-in")
	}
	worker := exec.Command(os.Args[0], "-test.run=^TestADBSessionWorkerHelper$")
	home := os.Getenv("TI_ADB_TEST_HOME")
	if home == "" {
		home = t.TempDir()
	}
	worker.Env = append(os.Environ(), "TI_ADB_WORKER_HELPER=1", "ANDROID_USER_HOME="+home)
	s, err := openADBSession(worker, path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	return s
}

func TestNativeSessionRestartUsesFreshOwnedServer(t *testing.T) {
	for i := 0; i < 3; i++ {
		s := nativeSession(t)
		if out, err := s.Run("", "devices"); err != nil || !strings.Contains(out, "List of devices") {
			t.Fatal(out, err)
		}
		s.Close()
		if adbServerReady(s.Port) {
			t.Fatal("owned native server survived application exit", s.PID)
		}
		if _, err := s.Run("", "devices"); err == nil || adbServerReady(s.Port) {
			t.Fatal("closed session automatically restarted a daemon")
		}
	}
}

func TestNativeSessionsDoNotInterruptEachOther(t *testing.T) {
	a, b := nativeSession(t), nativeSession(t)
	if a.Port == b.Port || a.PID == b.PID {
		t.Fatal("sessions shared an ADB daemon")
	}
	a.Close()
	if !adbServerReady(b.Port) {
		t.Fatal("closing one tool interrupted another")
	}
}

func TestNativeSessionDoesNotAdoptAnotherDaemonAfterNativeCrash(t *testing.T) {
	s := nativeSession(t)
	native, err := os.FindProcess(s.PID)
	if err != nil {
		t.Fatal(err)
	}
	if err = native.Kill(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-s.done:
	case <-time.After(3 * time.Second):
		t.Fatal("supervisor did not report the native failure")
	}
	if _, err = s.Run("", "devices"); err == nil || adbServerReady(s.Port) {
		t.Fatal("a dead native session was silently replaced")
	}
}

func TestADBSessionParentHelper(t *testing.T) {
	if os.Getenv("TI_ADB_PARENT_HELPER") != "1" {
		return
	}
	s := nativeSession(t)
	json.NewEncoder(os.Stdout).Encode(adbReady{Port: s.Port, PID: s.PID})
	<-time.After(time.Hour)
}

func TestNativeSessionReapsADBWhenApplicationIsKilled(t *testing.T) {
	if os.Getenv("TI_NATIVE_ADB") == "" {
		t.Skip("native ADB integration opt-in")
	}
	parent := exec.Command(os.Args[0], "-test.run=^TestADBSessionParentHelper$")
	parent.Env = append(os.Environ(), "TI_ADB_PARENT_HELPER=1", "TI_ADB_TEST_HOME="+t.TempDir())
	output, err := parent.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	parent.Stderr = os.Stderr
	if err = parent.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { parent.Process.Kill(); parent.Wait() }()
	var ready adbReady
	if err = json.NewDecoder(output).Decode(&ready); err != nil || ready.Port == "" {
		t.Fatal(ready, err)
	}
	parent.Process.Kill()
	deadline := time.Now().Add(5 * time.Second)
	for adbServerReady(ready.Port) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if adbServerReady(ready.Port) {
		t.Fatal("orphan native ADB survived abrupt application exit", ready)
	}
}

func TestGracefulExitWaitsForInstallOrRestore(t *testing.T) {
	a := newTestApp(t)
	a.op.Lock()
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: http.NotFoundHandler()}
	go server.Serve(ln)
	drained := make(chan struct{})
	go func() { drainInstaller(server, a); close(drained) }()
	select {
	case <-drained:
		t.Fatal("exit interrupted the running operation")
	case <-time.After(30 * time.Millisecond):
	}
	a.op.Unlock()
	select {
	case <-drained:
	case <-time.After(time.Second):
		t.Fatal("exit did not finish after the operation completed")
	}
}

type guardReplyRunner struct {
	Runner
	status string
}

func (r guardReplyRunner) Run(serial string, args ...string) (string, error) {
	if strings.Contains(strings.Join(args, " "), "guard_status") {
		return `Broadcast completed: result=200, data="` + r.status + `"` + "\n" + rcMarker + "0", nil
	}
	return r.Runner.Run(serial, args...)
}

func TestGuardStatusUsesRealServiceConnection(t *testing.T) {
	for _, bound := range []string{"true", "false"} {
		r := guardReplyRunner{Runner: fake(), status: "hotel_guard bound=" + bound + " windows=0"}
		want := "unbound"
		if bound == "true" {
			want = "bound"
		}
		if got := guardBinding(r, "192.168.4.70:5555"); got != want {
			t.Fatal(bound, got)
		}
	}
}
