package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// A copy of each restore point lives on the TV itself (cleanyourtv-app writes
// /sdcard/cleanyourtv/restore-point.json): another installer PC, or one whose data/ was lost, can
// still return a first-install TV to its vendor launcher. It holds no credential, PIN or APK.
const tvRestoreDir = "/sdcard/hotel-tv-restore"

func (e Engine) pushRestorePoint(target string, s Snapshot) bool {
	tmp := filepath.Join(e.State, "tmp")
	if os.MkdirAll(tmp, 0700) != nil {
		return false
	}
	if _, err := query(e.Runner, target, "mkdir", "-p", tvRestoreDir); err != nil {
		return false
	}
	write := func(v Snapshot, remote string) bool {
		local := filepath.Join(tmp, "restore-point.json")
		defer os.Remove(local)
		if atomicJSON(local, v) != nil {
			return false
		}
		_, err := e.Runner.Run(target, "push", local, tvRestoreDir+"/"+remote)
		return err == nil
	}
	ok := write(s, s.ID+".json")
	// origin.json: first record wins on the TV as well. It is written only when the TV clearly has
	// none, and never from a record that already shows our client installed.
	out, err := query(e.Runner, target, "ls", tvRestoreDir+"/origin.json")
	if err != nil && strings.Contains(strings.ToLower(out), "no such file") {
		origin := s
		if s.Origin != "" {
			if o, oe := e.loadSnapshot(s.Origin); oe == nil {
				origin = o
			}
		}
		if !origin.Polluted {
			ok = write(origin, "origin.json") && ok
		}
	}
	return ok
}

var (
	packageName   = regexp.MustCompile(`^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$`)
	componentName = regexp.MustCompile(`^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*/[A-Za-z0-9_.$]+$`)
)

// readTVOrigin reads the TV's origin.json as untrusted input: only the fields a restore needs are
// taken, each validated against what the TV itself reports now.
func (e Engine) readTVOrigin(target string, f Facts, identity string) (Snapshot, error) {
	var s Snapshot
	remote := tvRestoreDir + "/origin.json"
	size, err := query(e.Runner, target, "stat", "-c", "%s", remote)
	n, convErr := strconv.Atoi(strings.TrimSpace(size))
	if err != nil || convErr != nil {
		return s, errors.New("电视上没有本工具写入的还原点（1.3.0 起安装的电视才有）")
	}
	if n <= 0 || n > 256<<10 {
		return s, errors.New("电视上的还原点大小异常，拒绝读取")
	}
	tmp := filepath.Join(e.State, "tmp")
	if err = os.MkdirAll(tmp, 0700); err != nil {
		return s, err
	}
	local := filepath.Join(tmp, "tv-origin.json")
	defer os.Remove(local)
	if _, err = e.Runner.Run(target, "pull", remote, local); err != nil {
		return s, errors.New("电视上的还原点无法读取")
	}
	b, err := os.ReadFile(local)
	var raw Snapshot
	if err != nil || len(b) > 256<<10 || json.Unmarshal(b, &raw) != nil {
		return s, errors.New("电视上的还原点无法读取")
	}
	if !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(raw.ID) || raw.Identity == "" || raw.Identity != identity || raw.Facts.Model != f.Model {
		return s, errors.New("电视上的还原点不属于这台电视，拒绝导入")
	}
	home, err := readHomeFor(e.Runner, target, f.SDK)
	if err != nil {
		return s, err
	}
	known := home.Resolved == raw.Home
	for _, c := range home.Candidates {
		known = known || c == raw.Home
	}
	if !componentName.MatchString(raw.Home) || isPlaceholderHome(raw.Home) || componentPackage(raw.Home) == pkg || !known {
		return s, errors.New("电视上的还原点记录的原厂桌面不是这台电视现有的桌面，拒绝导入")
	}
	if raw.VendorHome != "" && !packageName.MatchString(raw.VendorHome) {
		return s, errors.New("电视上的还原点含无效的厂商桌面属性，拒绝导入")
	}
	clean := func(list string) string {
		var keep []string
		for _, c := range services(list) {
			if componentName.MatchString(c) && componentPackage(c) != pkg {
				keep = append(keep, c)
			}
		}
		return strings.Join(keep, ":")
	}
	enabled := raw.AccessibilityEnabled
	if enabled != "0" && enabled != "1" {
		enabled = "null"
	}
	at := ""
	if t, te := time.Parse(time.RFC3339, raw.At); te == nil {
		at = t.Format(time.RFC3339)
	}
	s = Snapshot{ID: raw.ID, Target: target, At: at, Facts: raw.Facts, Home: raw.Home, HomeResolved: raw.HomeResolved, Accessibility: clean(raw.Accessibility), AccessibilityEnabled: enabled,
		Notification: clean(raw.Notification), VendorHome: raw.VendorHome, Installed: raw.Installed, APKHashes: map[string]string{}, Identity: identity, Tool: raw.Tool, FromTV: true}
	if s.HomeResolved != "" && s.HomeResolved != s.Home {
		s.HomeResolved = s.Home
	}
	if validACSnapshot(raw.NativeACPower) && verifiedACPower(f) && raw.Facts.Fingerprint == f.Fingerprint {
		power := *raw.NativeACPower
		s.NativeACPower = &power
		s.ACPowerRequested = true
	}
	if raw.Installed {
		for name, h := range raw.APKHashes {
			if regexp.MustCompile(`^previous-[0-9]+\.apk$`).MatchString(name) && regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(h) {
				s.APKs = append(s.APKs, name)
				s.APKHashes[name] = h
			}
		}
		sort.Strings(s.APKs)
	}
	return s, nil
}

// importTVRestorePoint reads origin.json from the TV into this PC's backups after checking it
// belongs to this very TV. Upgrade records still need the original PC's APK files.
func (e Engine) importTVRestorePoint(raw string) (Snapshot, error) {
	var s Snapshot
	target, err := validTarget(raw)
	if err != nil {
		return s, err
	}
	if err = connect(e.Runner, target); err != nil {
		return s, err
	}
	f, err := inspect(e.Runner, target)
	if err != nil {
		return s, err
	}
	id, ie := e.tvIdentity(target)
	if ie != nil {
		return s, errors.New("无法读取电视身份")
	}
	if s, err = e.readTVOrigin(target, f, id); err != nil {
		return s, err
	}
	if existing, le := e.loadSnapshot(s.ID); le == nil {
		if existing.Identity != id {
			return s, errors.New("本机同编号备份属于另一台电视，拒绝导入")
		}
		existing.Target = target // the TV may have a new address or debugging port
		return existing, e.saveSnapshot(existing)
	}
	if s.Installed {
		for _, name := range s.APKs {
			if _, se := os.Stat(filepath.Join(e.State, "backups", s.ID, name)); se != nil {
				return s, errors.New("这台电视安装前已有旧影院客户端，回装旧APK需要原施工电脑 data/backups 里的备份文件")
			}
		}
	}
	return s, e.saveSnapshot(s)
}

func (e Engine) findOrigin(identity, exclude string) string {
	entries, _ := os.ReadDir(filepath.Join(e.State, "backups"))
	bestID, bestAt := "", time.Time{}
	for _, d := range entries {
		if !d.IsDir() || d.Name() == exclude {
			continue
		}
		s, err := e.loadSnapshot(d.Name())
		// Records read back from a TV never decide ordering: their timestamp is not trusted.
		if err != nil || s.Identity != identity || s.FromTV {
			continue
		}
		at, err := time.Parse(time.RFC3339, s.At)
		if err != nil {
			continue
		}
		if bestID == "" || at.Before(bestAt) {
			bestID, bestAt = s.ID, at
			if s.Origin != "" {
				bestID = s.Origin
			}
		}
	}
	return bestID
}

// Field records are this hotel's own evidence per firmware: what was tried and whether the cold
// boot check held. They become the graduation evidence for adding a verified catalog entry
// (cleanyourtv-app: confirmations per brand and Android version). No IP, identity or secret.
type FieldRecord struct {
	At             string `json:"at"`
	Tool           string `json:"tool"`
	Brand          string `json:"brand"`
	Model          string `json:"model"`
	Device         string `json:"device"`
	SDK            int    `json:"sdk"`
	Fingerprint    string `json:"fingerprint"`
	Tier           string `json:"tier,omitempty"`
	Route          string `json:"route,omitempty"`
	Result         string `json:"result"`
	RebootVerified bool   `json:"reboot_verified"`
	Message        string `json:"message,omitempty"`
}

func (e Engine) recordField(f Facts, s Snapshot, err error) {
	if f.Model == "" || e.State == "" {
		return
	}
	r := FieldRecord{At: time.Now().Format(time.RFC3339), Tool: version, Brand: f.Brand, Model: f.Model, Device: f.Device, SDK: f.SDK, Fingerprint: f.Fingerprint, Tier: s.Tier, Route: s.Route, RebootVerified: s.RebootVerified}
	switch {
	case err != nil:
		r.Result, r.Message = "failed", err.Error()
	case s.RebootUnreachable:
		r.Result = "reboot_unverified"
	default:
		r.Result = "installed"
	}
	b, _ := json.Marshal(r)
	path := filepath.Join(e.State, "field-records.jsonl")
	file, ferr := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if ferr != nil {
		return
	}
	defer file.Close()
	file.Write(append(b, '\n'))
}

// fieldHistory counts this data directory's installs of the exact firmware.
func (e Engine) fieldHistory(f Facts) (ok, failed int) {
	file, err := os.Open(filepath.Join(e.State, "field-records.jsonl"))
	if err != nil {
		return 0, 0
	}
	defer file.Close()
	sc := bufio.NewScanner(file)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		var r FieldRecord
		if json.Unmarshal([]byte(strings.TrimSpace(sc.Text())), &r) != nil || r.Fingerprint != f.Fingerprint {
			continue
		}
		if r.Result == "installed" && r.RebootVerified {
			ok++
		} else if r.Result == "failed" {
			failed++
		}
	}
	return ok, failed
}
