package main

import (
	"errors"
	"regexp"
	"strings"
	"time"
)

// HOME queries always carry CATEGORY_DEFAULT. Without it, firmwares whose FallbackHome lacks
// DEFAULT answer with a different candidate set and Android drops the stored default launcher
// (Aerial #767 on XGIMI m7642) — the backup step itself must never change the TV.
var homeIntent = []string{"-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME", "-c", "android.intent.category.DEFAULT"}

var componentLine = regexp.MustCompile(`^\s*([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*/[A-Za-z0-9_.$]+)\s*$`)

type HomeInfo struct {
	Raw        string   `json:"raw,omitempty"`
	Resolved   string   `json:"resolved,omitempty"`
	Candidates []string `json:"candidates,omitempty"`
	// True when the system has no explicit default (chooser) or answers with a placeholder.
	Placeholder bool `json:"placeholder,omitempty"`
}

// Placeholders are never recorded as the TV's own launcher (cleanyourtv-app HomePlaceholder.kt).
func isPlaceholderHome(component string) bool {
	c := strings.ToLower(component)
	for _, p := range []string{"resolveractivity", "chooseractivity", "fallbackhome", "setupwraith", "provision", "setupwizard", "cryptkeeper", "com.android.internal.app"} {
		if strings.Contains(c, p) {
			return true
		}
	}
	return false
}

func homeComponent(out string) string {
	last := ""
	for _, line := range strings.Split(out, "\n") {
		if m := componentLine.FindStringSubmatch(line); m != nil {
			last = m[1]
		}
	}
	if last != "" {
		return last
	}
	// Android 7.x has no --brief: take ActivityInfo packageName + name from the full dump.
	pkgName := regexp.MustCompile(`(?m)^\s*packageName=([A-Za-z0-9_.]+)\s*$`).FindStringSubmatch(out)
	name := regexp.MustCompile(`(?m)^\s*name=([A-Za-z0-9_.$]+)\s*$`).FindStringSubmatch(out)
	if pkgName != nil && name != nil {
		cls := name[1]
		if strings.HasPrefix(cls, pkgName[1]+".") {
			cls = cls[len(pkgName[1]):]
		}
		return pkgName[1] + "/" + cls
	}
	return ""
}

func componentPackage(c string) string { return strings.SplitN(c, "/", 2)[0] }

// componentsFromDump pairs name= and packageName= inside each ActivityInfo block of a full
// (non --brief) ResolveInfo dump.
func componentsFromDump(out string) []string {
	var comps []string
	seen := map[string]bool{}
	name, pkgName := "", ""
	flush := func() {
		if name != "" && pkgName != "" {
			cls := name
			if strings.HasPrefix(cls, pkgName+".") {
				cls = cls[len(pkgName):]
			}
			if c := pkgName + "/" + cls; componentLine.MatchString(c) && !seen[c] {
				seen[c] = true
				comps = append(comps, c)
			}
		}
		name, pkgName = "", ""
	}
	nameRe := regexp.MustCompile(`^\s*name=([A-Za-z0-9_.$]+)\s*$`)
	pkgRe := regexp.MustCompile(`^\s*packageName=([A-Za-z0-9_.]+)\s*$`)
	for _, line := range strings.Split(out, "\n") {
		if strings.Contains(line, "ActivityInfo:") {
			flush()
			continue
		}
		if m := nameRe.FindStringSubmatch(line); m != nil && name == "" {
			name = m[1]
		} else if m := pkgRe.FindStringSubmatch(line); m != nil && pkgName == "" {
			pkgName = m[1]
		}
	}
	flush()
	return comps
}

// legacyHomeRe finds the HOME task's component in `dumpsys activity activities` of Android 5–6,
// which prints the launcher task as "intent={act=… cat=[android.intent.category.HOME] … cmp=…}".
var legacyHomeRe = regexp.MustCompile(`cat=\[[^\]\n]*android\.intent\.category\.HOME[^\]\n]*\][^\n}]*?cmp=([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*/[A-Za-z0-9_.$]+)`)

// readHomeFor reads the TV's launcher with the method its Android version supports. Android 5–6 have
// no `cmd package`: the running HOME task is the record, and the installer never changes the default.
func readHomeFor(r Runner, target string, sdk int) (HomeInfo, error) {
	if sdk >= cmdPackageSDK {
		return readHome(r, target)
	}
	var h HomeInfo
	out, err := query(r, target, "dumpsys", "activity", "activities")
	if err != nil {
		return h, errors.New("无法读取系统任务记录以确认原厂桌面")
	}
	seen := map[string]bool{}
	for _, m := range legacyHomeRe.FindAllStringSubmatch(out, -1) {
		if !seen[m[1]] {
			seen[m[1]] = true
			h.Candidates = append(h.Candidates, m[1])
		}
	}
	for _, c := range h.Candidates {
		if componentPackage(c) != pkg && !isPlaceholderHome(c) {
			h.Resolved = c
			h.Raw = c
			break
		}
	}
	if h.Resolved == "" {
		return h, errors.New("无法从系统任务记录确认原厂桌面（Android 5–6）")
	}
	return h, nil
}

func readHome(r Runner, target string) (HomeInfo, error) {
	var h HomeInfo
	out, err := query(r, target, append([]string{"cmd", "package", "resolve-activity", "--brief"}, homeIntent...)...)
	if err != nil || homeComponent(out) == "" {
		out, err = query(r, target, append([]string{"cmd", "package", "resolve-activity"}, homeIntent...)...)
	}
	if err != nil {
		return h, errors.New("电视不支持查询默认桌面（需要 Android 7 以上的 cmd package）")
	}
	h.Raw = lastLines(out, 4)
	h.Resolved = homeComponent(out)
	if q, qe := query(r, target, append([]string{"cmd", "package", "query-activities", "--brief"}, homeIntent...)...); qe == nil {
		seen := map[string]bool{}
		for _, line := range strings.Split(q, "\n") {
			if m := componentLine.FindStringSubmatch(line); m != nil && !seen[m[1]] {
				seen[m[1]] = true
				h.Candidates = append(h.Candidates, m[1])
			}
		}
	}
	if len(h.Candidates) == 0 {
		// Android 7.x rejects --brief: read the full ResolveInfo dumps instead.
		if q, qe := query(r, target, append([]string{"cmd", "package", "query-activities"}, homeIntent...)...); qe == nil {
			h.Candidates = componentsFromDump(q)
		}
	}
	if h.Resolved == "" || isPlaceholderHome(h.Resolved) {
		h.Placeholder = true
	}
	return h, nil
}

// originalHome picks the launcher a restore should hand HOME back to.
func (h HomeInfo) originalHome() string {
	if !h.Placeholder && h.Resolved != "" {
		return h.Resolved
	}
	for _, c := range h.Candidates {
		if !isPlaceholderHome(c) && componentPackage(c) != pkg {
			return c
		}
	}
	return ""
}

func lastLines(s string, n int) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

var (
	// Most specific first: Android 10+ topResumedActivity, the display-level ResumedActivity line,
	// then the per-stack mResumedActivity of Android 7–9.
	resumedRes = []*regexp.Regexp{
		regexp.MustCompile(`(?m)topResumedActivity\s*[:=]\s*ActivityRecord\{[^}\n]*?\s([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)/`),
		regexp.MustCompile(`(?m)^\s*ResumedActivity\s*:\s*ActivityRecord\{[^}\n]*?\s([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)/`),
		regexp.MustCompile(`(?m)mResumedActivity\s*[:=]\s*ActivityRecord\{[^}\n]*?\s([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)/`),
	}
	focusRe = regexp.MustCompile(`(?m)mCurrentFocus=Window\{[^}\n]*?\s([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)/`)
	appRe   = regexp.MustCompile(`(?m)mFocusedApp=[^\n]*?\s([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)/`)
)

func resumedPackage(dump string) string {
	for _, re := range resumedRes {
		if m := re.FindStringSubmatch(dump); m != nil {
			return m[1]
		}
	}
	return ""
}

// foreground reads which package is in front from several sources, because each Android version
// and vendor prints a different subset: resumed activity (activities dump, Android 10+ prints
// topResumedActivity), then the focused window, then the focused app token. 1.2.0 read only
// mCurrentFocus from `dumpsys window windows`.
func foreground(r Runner, target string) (string, string, error) {
	if a, err := query(r, target, "dumpsys", "activity", "activities"); err == nil {
		if p := resumedPackage(a); p != "" {
			return p, "resumed", nil
		}
	}
	w, err := query(r, target, "dumpsys", "window")
	if err != nil {
		w, err = query(r, target, "dumpsys", "window", "windows")
	}
	if err != nil {
		return "", "", errors.New("无法读取电视前台界面")
	}
	if m := focusRe.FindStringSubmatch(w); m != nil {
		return m[1], "focus", nil
	}
	if m := appRe.FindStringSubmatch(w); m != nil {
		return m[1], "focused_app", nil
	}
	return "", "", errors.New("电视前台界面无法识别")
}

// waitForeground polls until want (or, with negate, anything but want) has stayed in front for
// `stable` consecutive reads. A launcher that the firmware pulls back seconds later must not pass
// (Aerial selftest.sh keeps 4 s after return; #849).
func (e Engine) waitForeground(target, want string, negate bool, attempts, stable int, every time.Duration) (string, error) {
	streak, last := 0, ""
	for i := 0; i < attempts; i++ {
		e.wait(every)
		got, _, err := foreground(e.Runner, target)
		if err != nil {
			streak = 0
			last = "无法读取"
			continue
		}
		last = got
		ok := got == want
		if negate {
			ok = got != want && got != ""
		}
		if ok {
			streak++
			if streak >= stable {
				return got, nil
			}
		} else {
			streak = 0
		}
	}
	return last, errors.New("前台未稳定")
}

// guardBinding reports whether the system actually bound our accessibility guard. The settings
// string can be written while the service stays unbound (restricted settings, firmware cleanup).
// "unknown" means the dump was unreadable on this firmware; callers then rely on the HOME test.
func guardBinding(r Runner, target string) string {
	out, err := query(r, target, "dumpsys", "activity", "services", pkg)
	if err != nil {
		return "unknown"
	}
	if strings.Contains(out, "HotelHomeGuardService") && strings.Contains(out, "ServiceRecord") {
		return "bound"
	}
	if strings.Contains(out, "ACTIVITY MANAGER SERVICES") || strings.Contains(out, "(nothing)") {
		return "unbound"
	}
	return "unknown"
}
