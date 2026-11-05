package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// Some firmware switches ADB debugging off at every boot. On MiTV-ASTP0 (OS3.0.104) the BOOT_COMPLETED
// handler of com.xiaomi.mitv.settings writes adb_enabled=0 (field log 2026-10-30 15:44:52). On such a
// TV a pre-check reboot can never prove a recovery channel: debugging is gone after every boot. The
// channel after install is the hotel client's own keep_adb (GuardSetup.ensureAdb, from Aerial), which
// puts ADB back on as soon as the system turns it off; the installer switches it on only for the
// verification and back off afterwards.

const adbOffFile = "adb-off-at-boot.json"

type adbOffRecord struct {
	At    string `json:"at"`
	Model string `json:"model"`
}

func (e Engine) adbOffKnown(identity string) bool {
	if identity == "" || e.State == "" {
		return false
	}
	m := map[string]adbOffRecord{}
	if b, err := os.ReadFile(filepath.Join(e.State, adbOffFile)); err == nil {
		json.Unmarshal(b, &m)
	}
	_, ok := m[identity]
	return ok
}

// rememberAdbOff stores the identity digest only (no IP, no android_id).
func (e Engine) rememberAdbOff(identity string, f Facts) {
	if identity == "" || e.State == "" {
		return
	}
	path := filepath.Join(e.State, adbOffFile)
	m := map[string]adbOffRecord{}
	if b, err := os.ReadFile(path); err == nil {
		json.Unmarshal(b, &m)
	}
	m[identity] = adbOffRecord{At: time.Now().Format(time.RFC3339), Model: f.Brand + " " + f.Model}
	atomicJSON(path, m)
}

var (
	logPID       = regexp.MustCompile(`^\d\d-\d\d (\d\d):(\d\d):(\d\d)\.(\d{3}) [VDIWEF]/[^(]*\(\s*(\d+)\)`)
	bootReceived = regexp.MustCompile(`BOOT_COMPLETED`)
	adbPutOff    = regexp.MustCompile(`putInt\(adb_enabled,\s*0\)`)
)

// adbOffInLog looks for this boot's evidence: a vendor settings process that, on BOOT_COMPLETED,
// wrote adb_enabled=0 within two seconds. A user switching ADB off in the menu writes the same
// value, but not right after BOOT_COMPLETED in the same process.
func adbOffInLog(out string) bool {
	type mark struct {
		pid string
		ms  int
	}
	var boots []mark
	for _, line := range strings.Split(out, "\n") {
		m := logPID.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		ms := ((atoi(m[1])*60+atoi(m[2]))*60+atoi(m[3]))*1000 + atoi(m[4])
		if bootReceived.MatchString(line) {
			boots = append(boots, mark{m[5], ms})
			continue
		}
		if adbPutOff.MatchString(line) {
			for _, b := range boots {
				if b.pid == m[5] && ms-b.ms >= 0 && ms-b.ms <= 2000 {
					return true
				}
			}
		}
	}
	return false
}

func atoi(s string) int {
	n := 0
	for _, c := range s {
		n = n*10 + int(c-'0')
	}
	return n
}

// adbOffSeenThisBoot reads only the vendor settings tags of the current log (small and read-only).
func (e Engine) adbOffSeenThisBoot(target string) bool {
	out, err := query(e.Runner, target, "logcat", "-d", "-v", "time", "-s", "Settings:D", "SettingsUtil:D")
	return err == nil && adbOffInLog(out)
}

func (e Engine) setKeepADB(target string, keep bool) {
	v := "false"
	if keep {
		v = "true"
	}
	shell(e.Runner, target, "am", "broadcast", "-a", pkg+".CONFIG", "-n", pkg+"/.guard.ConfigReceiver", "--ez", "keep_adb", v)
}
