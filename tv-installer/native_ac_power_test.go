package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type powerRunner struct {
	*fakeADB
	mode   string
	writes int
	refuse bool
}

func (p *powerRunner) Run(target string, args ...string) (string, error) {
	if len(args) > 1 && args[0] == "shell" && strings.Contains(args[1], "'PowerModeTool'") {
		body := "MODE_BEFORE=" + p.mode
		if !strings.Contains(args[1], "'read'") {
			want := "direct"
			if strings.Contains(args[1], "'restore-secondary'") {
				want = "secondary"
			}
			if p.mode != want {
				p.writes++
				if !p.refuse {
					p.mode = want
				}
				body += "\nNATIVE_RESULT=1"
			}
			body += "\nMODE_AFTER=" + p.mode
		}
		return rcWrap(args, body, nil)
	}
	return p.fakeADB.Run(target, args...)
}
func powerFixture(t *testing.T, before string) (Engine, *powerRunner, Snapshot) {
	t.Helper()
	f := fake()
	e := testEngine(t, f)
	p := &powerRunner{fakeADB: f, mode: before}
	e.Runner = p
	path := filepath.Join(e.Root, "helpers", "power-mode.jar")
	os.MkdirAll(filepath.Dir(path), 0700)
	os.WriteFile(path, []byte("test-only-dex"), 0600)
	if err := atomicJSON(filepath.Join(e.Root, "manifest.json"), map[string]any{"files": map[string]string{"helpers/power-mode.jar": digest([]byte("test-only-dex"))}}); err != nil {
		t.Fatal(err)
	}
	facts := Facts{Model: "MiTV-ASTP0", Device: "mulan", SDK: 28, Fingerprint: "Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys"}
	s, err := e.snapshot("192.168.4.62:5555", facts)
	if err != nil {
		t.Fatal(err)
	}
	s.ACPowerRequested = true
	if err = e.captureACPower(s.Target, &s); err != nil {
		t.Fatal(err)
	}
	if err = e.saveSnapshot(s); err != nil {
		t.Fatal(err)
	}
	return e, p, s
}
func TestACPowerPreservesActualOriginalAndReadsBack(t *testing.T) {
	for _, before := range []string{"secondary", "direct"} {
		t.Run(before, func(t *testing.T) {
			e, p, s := powerFixture(t, before)
			if err := e.applyACPower(s.Target, &s, func(string) {}); err != nil {
				t.Fatal(err)
			}
			if p.mode != "direct" || s.NativeACPower.Before != before || s.NativeACPower.Readback != "direct" {
				t.Fatal(p.mode, s.NativeACPower)
			}
			if before == "direct" && p.writes != 0 {
				t.Fatal("already enabled was rewritten")
			}
			var failures []string
			e.restoreACPower(s.Target, s.Facts, s, &failures)
			if len(failures) > 0 || p.mode != before {
				t.Fatal(failures, p.mode, before)
			}
		})
	}
}
func TestACPowerPersistenceFailureAndTamperNeverWrite(t *testing.T) {
	e, p, s := powerFixture(t, "secondary")
	bad := filepath.Join(t.TempDir(), "not-a-directory")
	os.WriteFile(bad, []byte("x"), 0600)
	e.State = bad
	if e.applyACPower(s.Target, &s, func(string) {}) == nil || p.writes != 0 {
		t.Fatal("write without persisted restore point")
	}
	e, p, s = powerFixture(t, "secondary")
	os.WriteFile(filepath.Join(e.Root, "helpers", "power-mode.jar"), []byte("tampered"), 0600)
	if e.applyACPower(s.Target, &s, func(string) {}) == nil || p.writes != 0 {
		t.Fatal("tampered helper used")
	}
}
func TestACPowerRefusalUnknownAndFirmwareChange(t *testing.T) {
	e, p, s := powerFixture(t, "secondary")
	p.refuse = true
	if e.applyACPower(s.Target, &s, func(string) {}) == nil {
		t.Fatal("write not applied counted as success")
	}
	e, p, s = powerFixture(t, "secondary")
	p.mode = "unknown"
	if e.applyACPower(s.Target, &s, func(string) {}) == nil || p.writes != 0 {
		t.Fatal("unknown current mode written")
	}
	e, p, s = powerFixture(t, "secondary")
	s.NativeACPower.Attempted = true
	changed := s.Facts
	changed.Fingerprint += "-new"
	var failures []string
	e.restoreACPower(s.Target, changed, s, &failures)
	if len(failures) == 0 || p.writes != 0 {
		t.Fatal("firmware mismatch not refused")
	}
}
func TestACPowerOldSnapshotAndMalformedReceipts(t *testing.T) {
	e, p, s := powerFixture(t, "secondary")
	s.NativeACPower = nil
	var failures []string
	e.restoreACPower(s.Target, s.Facts, s, &failures)
	if len(failures) > 0 || p.writes != 0 {
		t.Fatal("old snapshot invented an original value")
	}
	for _, value := range []string{"", "MODE_BEFORE=unknown", "MODE_BEFORE=direct\nMODE_BEFORE=secondary"} {
		if _, err := modeLine(value, "MODE_BEFORE"); err == nil {
			t.Fatal("invalid native receipt accepted", value)
		}
	}
}

func TestFailedPowerSnapshotDoesNotAdvertiseUnwrittenBackup(t *testing.T) {
	e, p, _ := powerFixture(t, "secondary")
	p.model = "MiTV-ASTP0"
	p.device = "mulan"
	p.brand = "Xiaomi"
	p.fingerprint = "Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys"
	p.mode = "unknown"
	c, err := loadCatalog("catalog.json")
	if err != nil {
		t.Fatal(err)
	}
	e.Catalog = c
	s, err := e.install(InstallRequest{Target: "192.168.4.62:5555", ACPower: true}, strings.Repeat("a", 64), func(string) {})
	if err == nil {
		t.Fatal("unknown native mode accepted")
	}
	if !strings.Contains(err.Error(), "原厂模式回执") {
		t.Fatal("did not reach native snapshot failure", err)
	}
	if s.ID != "" || resultFor(s.Target, s, err, false).Backup != "" {
		t.Fatal("unsaved snapshot advertised as usable backup", s.ID)
	}
	if resultFor(s.Target, s, err, false).ACPower != "not_confirmed" {
		t.Fatal("read failure mislabelled as unsupported firmware")
	}
	if p.writes != 0 {
		t.Fatal("factory write before backup")
	}
}
