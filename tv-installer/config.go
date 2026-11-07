package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const pkg = "com.xjg.hoteltv"
const version = "2.0.4"
const activity = pkg + "/.MainActivity"
const guard = pkg + "/" + pkg + ".guard.HotelHomeGuardService"
const listener = pkg + "/" + pkg + ".guard.RevivalListener"

type Match struct {
	Models  []string `json:"models"`
	Devices []string `json:"devices"`
	Brand   string   `json:"brand"`
	// Brands: aliases for evidence records (getprop brand, Build.BRAND and manufacturer differ on
	// the same TV); brand or manufacturer must equal one of them.
	Brands            []string `json:"brands,omitempty"`
	Min               int      `json:"sdk_min"`
	Max               int      `json:"sdk_max"`
	Fingerprint       string   `json:"fingerprint,omitempty"`
	FingerprintPrefix string   `json:"fingerprint_prefix,omitempty"`
}
type Profile struct {
	ID         string `json:"id"`
	DeviceKind string `json:"device_kind,omitempty"`
	Name       string `json:"name"`
	Match      Match  `json:"match"`
	Route      string `json:"route"`
	Evidence   string `json:"evidence"`
	Tested     string `json:"tested"`
	Notes      string `json:"notes"`
	Level      string `json:"level"`
	// Grade of field evidence from elsewhere (Aerial, same guard code as our client):
	// proven = works with exact identifiers; limited = works with limits/non-transferable steps;
	// avoid = documented failure. Empty for this project's own and cleanyourtv records.
	Grade string `json:"grade,omitempty"`
}
type Platform struct {
	Name    string   `json:"name"`
	Brands  string   `json:"brands"`
	Status  string   `json:"status"`
	Reason  string   `json:"reason"`
	Sources []string `json:"sources"`
}
type Catalog struct {
	Schema     int        `json:"schema"`
	Version    string     `json:"version"`
	Profiles   []Profile  `json:"profiles"`
	Platforms  []Platform `json:"platforms"`
	Advisories []Advisory `json:"advisories,omitempty"`
}
type Facts struct {
	Brand        string `json:"brand"`
	Model        string `json:"model"`
	Device       string `json:"device"`
	SDK          int    `json:"sdk"`
	Firmware     string `json:"firmware"`
	Fingerprint  string `json:"fingerprint"`
	CryptoType   string `json:"crypto_type,omitempty"`
	Manufacturer string `json:"manufacturer,omitempty"`
}

// Decision tiers (1.3.0):
//
//	verified – this project's own field record for this exact firmware; one-click install.
//	trial    – an upstream record or no record: installable only when the operator ticks
//	           "允许试装", always with the reboot check, and auto-restored on failure.
//	blocked  – known not to work, or below what this tool can back up and restore.
type Decision struct {
	Allowed  bool     `json:"allowed"`
	Trial    bool     `json:"trial"`
	Avoid    bool     `json:"avoid,omitempty"`
	Tier     string   `json:"tier"`
	Route    string   `json:"route"`
	Reason   string   `json:"reason"`
	Warnings []string `json:"warnings,omitempty"`
	Profile  *Profile `json:"profile,omitempty"`
}

// Advisory is field knowledge without exact identifiers (mostly Aerial's README table, the same
// guard code our client forked). "warn" never blocks by itself; "block" does.
type Advisory struct {
	Brand          string   `json:"brand"`
	SDKMin         int      `json:"sdk_min"`
	SDKMax         int      `json:"sdk_max"`
	ModelsContains []string `json:"models_contains,omitempty"`
	Devices        []string `json:"devices,omitempty"`
	Level          string   `json:"level"`
	Note           string   `json:"note"`
	Source         string   `json:"source"`
}
type Hotel struct {
	PinHash string `json:"pin_hash"`
}

func validTarget(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	host, port := raw, "5555"
	if strings.Contains(raw, ":") {
		var err error
		host, port, err = net.SplitHostPort(raw)
		if err != nil {
			return "", errors.New("请输入电视 IPv4 或 IP:端口")
		}
	}
	ip := net.ParseIP(host)
	if ip == nil || ip.To4() == nil || !(ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast()) {
		return "", errors.New("仅接受局域网电视 IPv4 地址")
	}
	if !regexp.MustCompile(`^[0-9]{1,5}$`).MatchString(port) {
		return "", errors.New("端口无效")
	}
	var n int
	fmt.Sscanf(port, "%d", &n)
	if n < 1 || n > 65535 {
		return "", errors.New("端口无效")
	}
	return net.JoinHostPort(ip.String(), fmt.Sprint(n)), nil
}
func validOrigin(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil {
		return false
	}
	return (u.Scheme == "http" || u.Scheme == "https") && u.Hostname() != "" && u.User == nil && u.RawQuery == "" && u.Fragment == "" && (u.Path == "" || u.Path == "/") && !strings.ContainsAny(raw, "\n\r\x00")
}
func generatePin() (string, string, error) {
	var p strings.Builder
	for i := 0; i < 6; i++ {
		n, e := rand.Int(rand.Reader, big.NewInt(10))
		if e != nil {
			return "", "", e
		}
		p.WriteByte('0' + byte(n.Int64()))
	}
	hash := sha256.Sum256([]byte(p.String()))
	return p.String(), hex.EncodeToString(hash[:]), nil
}
func atomicJSON(path string, v any) error {
	data, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	if e = os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".write-*")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0600); e == nil {
		_, e = f.Write(data)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e != nil {
		return e
	}
	if ce != nil {
		return ce
	}
	// Windows cannot atomically replace an existing destination with os.Rename; the destination
	// is a local config, never a TV backup, so remove only after the replacement has been synced.
	if e = os.Rename(name, path); e != nil {
		if _, se := os.Stat(path); se == nil {
			if e = os.Remove(path); e != nil {
				return e
			}
			return os.Rename(name, path)
		}
		return e
	}
	return nil
}
func loadCatalog(path string) (Catalog, error) {
	var c Catalog
	b, e := os.ReadFile(path)
	if e != nil {
		return c, e
	}
	if len(b) > 3<<20 {
		return c, errors.New("机型库过大")
	}
	e = json.Unmarshal(b, &c)
	if e != nil {
		return c, e
	}
	if c.Schema != 1 || len(c.Profiles) == 0 {
		return c, errors.New("机型库版本无效")
	}
	for _, p := range c.Profiles {
		if p.ID == "" || (p.Route != "standard" && p.Route != "guard" && p.Route != "blocked") {
			return c, errors.New("机型库含未知安装路径")
		}
		if p.Grade != "" && p.Grade != "proven" && p.Grade != "limited" && p.Grade != "avoid" {
			return c, errors.New("机型库含未知证据级别")
		}
	}
	for _, a := range c.Advisories {
		if a.Level != "warn" && a.Level != "block" {
			return c, errors.New("机型库含未知提示级别")
		}
	}
	return c, nil
}

// Below Android 5.0 the hotel client itself cannot run (minSdk 21; the 4.0/4.4 builds in extras/
// are installed by hand). Android 5–6 have no `cmd package`, so the installer never changes the
// default HOME there: guard build only, and restore = remove guard + uninstall.
const minSupportedSDK = 21
const cmdPackageSDK = 24

func verifiedProfile(p Profile) bool {
	return strings.HasPrefix(p.ID, "hotel-") && p.Match.Fingerprint != ""
}

// fbeBootRisk: our MainActivity is directBootAware (forked from Aerial before 1.57). As the default
// HOME on a file-encrypted Android 10+ firmware it can start before unlock in place of the vendor
// FallbackHome; Aerial 1.47 left Redmi projectors stuck on the boot animation, recoverable only by
// factory reset (#684 #688). Models not verified by this project therefore use the guard build,
// which declares no HOME and cannot take part in boot.
func fbeBootRisk(f Facts) bool {
	return f.SDK >= 29 && strings.EqualFold(f.CryptoType, "file")
}

func advisoriesFor(c Catalog, f Facts) (warn []string, block string) {
	for _, a := range c.Advisories {
		if a.Brand != "*" && !strings.EqualFold(a.Brand, f.Brand) && !strings.EqualFold(a.Brand, f.Manufacturer) {
			continue
		}
		if (a.SDKMin > 0 && f.SDK < a.SDKMin) || (a.SDKMax > 0 && f.SDK > a.SDKMax) {
			continue
		}
		hit := len(a.ModelsContains) == 0 && len(a.Devices) == 0
		for _, m := range a.ModelsContains {
			if m != "" && strings.Contains(strings.ToLower(f.Model), strings.ToLower(m)) {
				hit = true
			}
		}
		for _, d := range a.Devices {
			if strings.EqualFold(d, f.Device) {
				hit = true
			}
		}
		if !hit {
			continue
		}
		line := a.Note
		if a.Source != "" {
			line += "（" + a.Source + "）"
		}
		if a.Level == "block" && block == "" {
			block = line
		} else {
			warn = append(warn, line)
		}
	}
	return warn, block
}

func profileMatches(p Profile, f Facts) bool {
	// Brand is optional: Aerial reports show getprop brand, Build.BRAND and manufacturer disagree
	// on the same TV (T966D5: TCL / IFFALCON), so evidence records match on model/device/SDK.
	if p.Match.Brand != "" && !strings.EqualFold(p.Match.Brand, f.Brand) {
		return false
	}
	if len(p.Match.Brands) > 0 {
		ok := false
		for _, b := range p.Match.Brands {
			ok = ok || strings.EqualFold(b, f.Brand) || strings.EqualFold(b, f.Manufacturer)
		}
		if !ok {
			return false
		}
	}
	if f.SDK < p.Match.Min || (p.Match.Max > 0 && f.SDK > p.Match.Max) {
		return false
	}
	modelMatch := len(p.Match.Models) == 0
	deviceMatch := len(p.Match.Devices) == 0
	for _, m := range p.Match.Models {
		if strings.EqualFold(m, f.Model) {
			modelMatch = true
		}
	}
	for _, d := range p.Match.Devices {
		if strings.EqualFold(d, f.Device) {
			deviceMatch = true
		}
	}
	if !modelMatch || !deviceMatch || len(p.Match.Models)+len(p.Match.Devices) == 0 {
		return false
	}
	if p.Match.FingerprintPrefix != "" && !strings.HasPrefix(f.Fingerprint, p.Match.FingerprintPrefix) {
		return false
	}
	return p.Match.Fingerprint == "" || p.Match.Fingerprint == f.Fingerprint
}

func evidenceLine(p Profile) string {
	line := p.Notes
	if p.Evidence != "" {
		line += "（" + p.Evidence + "）"
	}
	return line
}

func decide(c Catalog, f Facts) Decision {
	if f.SDK < minSupportedSDK {
		return Decision{Tier: "blocked", Reason: "暂不支持一键安装：影院客户端最低 Android 5.0；Android 4.0/4.4 兼容版在 extras/，需按其说明手动安装（Aerial 在 4.x 上同样用单独的 Lite 版、手动选“始终”）"}
	}
	warn, block := advisoriesFor(c, f)
	if block != "" {
		return Decision{Tier: "blocked", Reason: "暂不支持：" + block, Warnings: warn}
	}
	legacy := f.SDK < cmdPackageSDK
	guardOnly := legacy || fbeBootRisk(f)
	var verified, blocked, proven, avoid, others []Profile
	for _, p := range c.Profiles {
		if !profileMatches(p, f) {
			continue
		}
		switch {
		case verifiedProfile(p):
			verified = append(verified, p)
		case p.Grade == "proven":
			proven = append(proven, p)
		case p.Grade == "avoid":
			avoid = append(avoid, p)
		case p.Route == "blocked":
			blocked = append(blocked, p)
		default:
			others = append(others, p)
		}
	}
	extra := func(list ...[]string) []string {
		var out []string
		for _, l := range list {
			out = append(out, l...)
		}
		if legacy {
			out = append(out, "Android 5–6：只用不声明桌面的 guard 版、不改默认桌面（这条安装路线尚无真机验证，按试装处理）")
		} else if fbeBootRisk(f) {
			out = append(out, "Android 10+ 文件级加密且本项目未实测：使用不声明桌面的 guard 版，避开已知的开机卡死风险（Aerial #684）；开机时可能先出厂商桌面几秒再回到影院")
		}
		out = append(out, warn...)
		seen := map[string]bool{}
		uniq := out[:0]
		for _, w := range out {
			if !seen[w] {
				seen[w] = true
				uniq = append(uniq, w)
			}
		}
		return uniq
	}
	notes := func(ps []Profile) []string {
		var out []string
		for _, p := range ps {
			out = append(out, evidenceLine(p))
		}
		return out
	}
	if len(verified) > 0 {
		p := verified[0]
		route := p.Route
		if legacy {
			route = "guard"
		}
		return Decision{Allowed: true, Tier: "verified", Route: route, Profile: &p, Reason: "本项目已在这一固件实测：可一键安装；每台仍先预检重启、装完做重启验证（软件重启），拔电开机、遥控待机唤醒和播放需现场确认。", Warnings: warn}
	}
	if len(blocked) > 0 {
		p := blocked[0]
		return Decision{Tier: "blocked", Profile: &p, Reason: "暂不支持：" + p.Notes, Warnings: warn}
	}
	route := "standard"
	pick := func(p Profile) string {
		if guardOnly || p.Route == "guard" {
			return "guard"
		}
		return "standard"
	}
	switch {
	case len(proven) > 0 && len(avoid) > 0:
		p := proven[0]
		return Decision{Trial: true, Tier: "trial", Route: "guard", Profile: &p, Reason: "网上证据冲突：同一型号/系统既有成功也有失败的报告。可勾选“允许试装”，工具会做重启验证和 2 分钟稳定观察，失败自动还原", Warnings: extra(notes(proven), notes(avoid))}
	case len(proven) > 0:
		p := proven[0]
		if legacy || pick(p) != p.Route {
			// The evidence was gathered with another build or install path than the one this
			// tool must use here (Android 5–6 guard-only route, or guard forced on FBE): a trial.
			return Decision{Trial: true, Tier: "trial", Route: pick(p), Profile: &p, Reason: "网上已证，但证据用的是 Aerial 的 " + p.Route + " 版，而本工具在这台电视上必须改用 guard 版或安卓 5–6 新路线，证据不能完全沿用：可勾选“允许试装”，工具会做重启验证和稳定观察，失败自动还原", Warnings: extra(notes(proven))}
		}
		return Decision{Allowed: true, Tier: "proven", Route: pick(p), Profile: &p, Reason: "网上已证：Aerial（与本影院客户端同一套守护代码）在这款电视上实测可用，可一键安装；工具会强制重启验证和 2 分钟稳定观察，失败自动还原", Warnings: extra(notes(proven))}
	case len(avoid) > 0:
		p := avoid[0]
		return Decision{Trial: true, Avoid: true, Tier: "avoid", Route: "guard", Profile: &p, Reason: "网上已知失败（不建议）：Aerial 实测这款电视接管不稳定或不成功。仍可勾选“允许试装”并二次确认，工具只用 guard 版，强制重启验证和 2 分钟稳定观察，失败自动还原", Warnings: extra(notes(avoid))}
	case len(others) > 0:
		p := others[0]
		for _, o := range others {
			if o.Route == "guard" {
				route = "guard"
			}
		}
		if guardOnly {
			route = "guard"
		}
		var limited []Profile
		for _, o := range others {
			if o.Grade == "limited" {
				limited = append(limited, o)
			}
		}
		return Decision{Trial: true, Tier: "trial", Route: route, Profile: &p, Reason: "参考项目有这款电视的记录，但没有可直接沿用的成功证据：可勾选“允许试装”安装，工具会做重启验证和稳定观察，失败自动还原", Warnings: extra(notes(limited))}
	}
	if guardOnly {
		route = "guard"
	}
	return Decision{Trial: true, Tier: "trial", Route: route, Reason: "机型库没有这台电视的记录：可勾选“允许试装”安装；工具会做重启验证和稳定观察，失败自动还原到原厂桌面并卸载影院", Warnings: extra([]string{"请先点“只读体检”保存报告，以便把这个型号加入机型库"})}
}

func quote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }
func services(raw string) []string {
	var out []string
	for _, s := range strings.Split(strings.TrimSpace(raw), ":") {
		if s != "" && s != "null" {
			out = append(out, s)
		}
	}
	return out
}
func mergeService(raw, component string, add bool) string {
	out := []string{}
	for _, s := range services(raw) {
		if s != component {
			out = append(out, s)
		}
	}
	if add {
		out = append(out, component)
	}
	return strings.Join(out, ":")
}
