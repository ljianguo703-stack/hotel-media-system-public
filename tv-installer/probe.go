package main

import (
	"regexp"
	"strings"
	"time"
)

// Diagnosis is the read-only "体检" report. It changes nothing on the TV and keeps no identity,
// serial, IP, MAC or credential. Its excerpts are what a new replay test for this model is built
// from (cleanyourtv-app turns user reports into fixtures; aerial-tv asks reporters for report.sh).
type Diagnosis struct {
	Tool          string            `json:"tool"`
	At            string            `json:"at"`
	Facts         Facts             `json:"facts"`
	Decision      Decision          `json:"decision"`
	History       map[string]int    `json:"history"`
	ADBFeatures   string            `json:"adb_features,omitempty"`
	Checks        map[string]string `json:"checks"`
	Home          HomeInfo          `json:"home"`
	Foreground    string            `json:"foreground,omitempty"`
	ForegroundBy  string            `json:"foreground_by,omitempty"`
	Accessibility []string          `json:"accessibility_services,omitempty"`
	VendorProps   map[string]string `json:"vendor_props,omitempty"`
	Risks         []string          `json:"risks,omitempty"`
	Excerpts      map[string]string `json:"excerpts"`
}

var vendorProps = []string{"persist.vt.use_home_pkg", "persist.sys.launcher.component", "persist.tcl.bootappmode", "ro.crypto.type", "ro.build.characteristics", "ro.product.cpu.abi", "ro.build.version.release", "persist.sys.usb.config", "service.adb.tcp.port", "persist.adb.tcp.port"}

func excerpt(s string, keep func(string) bool, max int) string {
	var out []string
	for _, line := range strings.Split(s, "\n") {
		if keep(line) {
			out = append(out, strings.TrimSpace(line))
			if len(out) >= max {
				break
			}
		}
	}
	return strings.Join(out, "\n")
}

func (e Engine) diagnose(raw string) (Diagnosis, error) {
	d := Diagnosis{Tool: version, At: time.Now().Format(time.RFC3339), Checks: map[string]string{}, Excerpts: map[string]string{}, VendorProps: map[string]string{}, History: map[string]int{}}
	target, err := validTarget(raw)
	if err != nil {
		return d, err
	}
	if err = connect(e.Runner, target); err != nil {
		return d, err
	}
	f, err := inspect(e.Runner, target)
	if err != nil {
		return d, err
	}
	d.Facts = f
	d.Decision = decide(e.Catalog, f)
	d.History["installed_reboot_verified"], d.History["failed"] = e.fieldHistory(f)
	if feats, fe := e.Runner.Run(target, "features"); fe == nil {
		d.ADBFeatures = strings.TrimSpace(feats)
		if !strings.Contains(feats, "shell_v2") {
			d.Risks = append(d.Risks, "adbd 不支持 shell_v2：传输层不回传退出码（本工具已用结束标记取退出码）")
		}
	}
	check := func(name string, ok bool, detail string) {
		if ok {
			d.Checks[name] = "ok"
		} else {
			d.Checks[name] = "fail: " + detail
		}
	}
	_, rc, rcErr := shellRC(e.Runner, target, "true")
	check("exit_status_marker", rcErr == nil && rc == 0, "设备 shell 不能回报退出状态")
	_, cmdErr := query(e.Runner, target, "cmd", "package", "list", "packages", "android")
	check("cmd_package", cmdErr == nil || f.SDK < cmdPackageSDK, "没有 cmd package（厂商裁剪）")
	if f.SDK < cmdPackageSDK {
		d.Risks = append(d.Risks, "Android 5–6：只能走 guard 版、不改默认桌面；这条路线尚无真机验证")
	}
	if f.SDK >= 29 {
		_, roleErr := query(e.Runner, target, "cmd", "role", "get-role-holders", "--user", "0", "android.app.role.HOME")
		check("cmd_role", roleErr == nil, "没有 role 服务或命令（Unknown command: get-role-holders 见 Aerial obe-r3-ultra.md）")
	}
	_, nErr := query(e.Runner, target, "cmd", "notification", "help")
	check("cmd_notification", nErr == nil, "不能用命令授权通知监听，守护复活退回同步适配器")
	_, aoErr := query(e.Runner, target, "appops", "get", "android", "SYSTEM_ALERT_WINDOW")
	check("appops", aoErr == nil, "appops 命令不可用")
	h, hErr := readHomeFor(e.Runner, target, f.SDK)
	d.Home = h
	check("home_query", hErr == nil && h.originalHome() != "", "无法确认原厂桌面")
	if h.Placeholder {
		d.Risks = append(d.Risks, "系统当前没有明确的默认桌面（选择框/占位界面）："+h.Resolved)
	}
	for _, c := range h.Candidates {
		if strings.Contains(strings.ToLower(c), "fallbackhome") && !strings.HasPrefix(c, "com.android.settings/") && !strings.HasPrefix(c, "com.android.tv.settings/") {
			d.Risks = append(d.Risks, "存在厂商自定义 FallbackHome："+c+"（Aerial #684 同类开机流程，standard 版有开机卡死风险）")
		}
	}
	if fg, by, fe := foreground(e.Runner, target); fe == nil {
		d.Foreground, d.ForegroundBy = fg, by
		check("foreground", true, "")
	} else {
		check("foreground", false, fe.Error())
	}
	if acc, ae := shell(e.Runner, target, "settings", "get", "secure", "enabled_accessibility_services"); ae == nil {
		d.Accessibility = services(acc)
		check("settings_secure", true, "")
	} else {
		check("settings_secure", false, "无法读取安全设置")
	}
	if fbeBootRisk(f) {
		d.Risks = append(d.Risks, "Android 10+ 文件级加密：未验证时只走 guard 版")
	}
	for _, p := range vendorProps {
		if v, ve := query(e.Runner, target, "getprop", p); ve == nil && strings.TrimSpace(v) != "" {
			d.VendorProps[p] = strings.TrimSpace(v)
		}
	}
	if v, ve := query(e.Runner, target, "settings", "get", "global", "verifier_verify_adb_installs"); ve == nil && strings.TrimSpace(v) == "1" {
		d.Risks = append(d.Risks, "系统会校验 ADB 安装（TCL 类 VERIFICATION_FAILURE 风险），工具会自动改用 guard 版重试")
	}
	if df, de := query(e.Runner, target, "df", "/data"); de == nil {
		d.Excerpts["df_data"] = lastLines(df, 2)
	}
	paths, perr := clientPaths(e.Runner, target)
	switch {
	case perr != nil:
		d.Checks["client"] = "fail: " + perr.Error()
	case len(paths) > 0:
		d.Checks["client"] = "installed"
	default:
		d.Checks["client"] = "absent"
	}
	d.Excerpts["resolve_home"] = h.Raw
	focusLine := regexp.MustCompile(`mCurrentFocus|mFocusedApp|ResumedActivity`)
	if w, we := query(e.Runner, target, "dumpsys", "window"); we == nil {
		d.Excerpts["window_focus"] = excerpt(w, focusLine.MatchString, 6)
	}
	if a, ae := query(e.Runner, target, "dumpsys", "activity", "activities"); ae == nil {
		d.Excerpts["activity_resumed"] = excerpt(a, focusLine.MatchString, 6)
	}
	return d, nil
}
