package main

import (
	"regexp"
	"sort"
	"strings"
)

// ClientGrants is what an already-installed hotel client held before this run. Restoring an
// upgraded TV puts the older APK back, and the grants this run added must go with it: with
// WRITE_SECURE_SETTINGS the client's BootReceiver, sync adapter and notification listener switch the
// guard back on at every boot (GuardSetup.ensureEnabled), and on Android 10+ the overlay op lets its
// BootReceiver bring it to the front whenever the guard is off. A client that was installed but not
// taking over would take the TV back at the next boot (Codex review of 1.3.0, finding 1).
// The permission and the app ops are recorded separately: either can be unreadable on its own.
type ClientGrants struct {
	WSSKnown bool              `json:"wss_known"`
	WSS      bool              `json:"write_secure_settings"`
	OpsKnown bool              `json:"appops_known"`
	AppOps   map[string]string `json:"appops,omitempty"`
}

const wssPermission = "android.permission.WRITE_SECURE_SETTINGS"

// touchedOps are the app ops install sets for the client.
func touchedOps(sdk int) []string {
	ops := []string{"SYSTEM_ALERT_WINDOW", "GET_USAGE_STATS", "APP_AUTO_START", "APP_ASSOC_START", "AUTO_START", "ASSOC_START"}
	if sdk >= 33 {
		ops = append(ops, "ACCESS_RESTRICTED_SETTINGS")
	}
	return ops
}

// takeoverOps decide whether the client can come back on its own; the rest are vendor extras whose
// names differ by firmware and may not exist at all.
var takeoverOps = map[string]bool{"SYSTEM_ALERT_WINDOW": true, "ACCESS_RESTRICTED_SETTINGS": true}

var opModes = map[string]bool{"allow": true, "ignore": true, "deny": true, "default": true, "foreground": true}

// `appops get <pkg>` prints one "OP: mode; time=…" line per op that is not default. Lines such as
// "Uid mode: …" (Android 10+) start with a mixed-case word and are skipped.
var opLine = regexp.MustCompile(`^\s*([A-Z][A-Z0-9_]+):\s*([a-z]+)\b`)

func parseAppOps(out string) map[string]string {
	m := map[string]string{}
	for _, line := range strings.Split(out, "\n") {
		if mm := opLine.FindStringSubmatch(line); mm != nil && opModes[mm[2]] {
			if _, seen := m[mm[1]]; !seen {
				m[mm[1]] = mm[2]
			}
		}
	}
	return m
}

var sectionHeader = regexp.MustCompile(`^[A-Za-z][A-Za-z ]*:$`)

// wssGranted reads the grant from `dumpsys package <pkg>`. Android 6+ lists it under "install
// permissions:" or "runtime permissions:" with granted=true|false; Android 5 has a
// "grantedPermissions:" list. The "requested permissions:" list names it without a grant and must
// not count. known is false when the dump does not show this package at all.
func wssGranted(dump string) (granted, known bool) {
	section, ours := "", false
	for _, raw := range strings.Split(strings.ReplaceAll(dump, "\r", ""), "\n") {
		line := strings.TrimSpace(raw)
		if strings.HasPrefix(line, "Package [") {
			ours = strings.HasPrefix(line, "Package ["+pkg+"]")
			known = known || ours
			section = ""
			continue
		}
		if sectionHeader.MatchString(line) {
			section = strings.ToLower(line)
			continue
		}
		if !ours || !strings.HasPrefix(line, wssPermission) {
			continue
		}
		rest := strings.TrimPrefix(line, wssPermission)
		switch section {
		case "install permissions:", "runtime permissions:":
			if strings.HasPrefix(rest, ":") && strings.Contains(rest, "granted=true") {
				granted = true
			}
		case "grantedpermissions:":
			if rest == "" {
				granted = true
			}
		}
	}
	return granted, known
}

// readGrants records the installed client's permission and app-op state. It only reads.
func (e Engine) readGrants(target string, sdk int) *ClientGrants {
	g := &ClientGrants{AppOps: map[string]string{}}
	if dump, err := query(e.Runner, target, "dumpsys", "package", pkg); err == nil {
		g.WSS, g.WSSKnown = wssGranted(dump)
	}
	if out, err := query(e.Runner, target, "appops", "get", pkg); err == nil && !refusal(out) {
		current := parseAppOps(out)
		for _, op := range touchedOps(sdk) {
			mode := current[op]
			if mode == "" {
				mode = "default"
			}
			g.AppOps[op] = mode
		}
		g.OpsKnown = true
	}
	return g
}

// grantPlan is the permission state a restore puts the older client back into.
type grantPlan struct {
	WSS         bool
	WSSInferred bool
	Ops         map[string]string // ops to set; nil = leave as they are
	OpsInferred bool
}

// planGrants uses the record where it was read. Where it was not (1.2.0 and 1.3.0 records, or a
// TV that would not say), it infers from whether the client was taking over before: without a
// takeover nothing is left that would let it start one. A record read back from the TV is
// untrusted and can never keep or grant anything.
func planGrants(s Snapshot) grantPlan {
	var p grantPlan
	g := s.Grants
	// Direct evidence only: a guess must never add a takeover the client did not have.
	takeover := hasGuard(s.Accessibility) || componentPackage(s.Home) == pkg || s.VendorHome == pkg
	if s.FromTV {
		g, takeover = nil, false
	}
	if g != nil && g.WSSKnown {
		p.WSS = g.WSS
	} else {
		p.WSS, p.WSSInferred = takeover, true
	}
	switch {
	case g != nil && g.OpsKnown:
		p.Ops = map[string]string{}
		for op, mode := range g.AppOps {
			p.Ops[op] = mode
		}
	case p.WSS:
		p.OpsInferred = true // a client that was taking over keeps what it has
	default:
		p.OpsInferred = true
		p.Ops = map[string]string{}
		for _, op := range touchedOps(s.Facts.SDK) {
			p.Ops[op] = "default"
		}
	}
	return p
}

// restoreClientGrants puts the client's permission and app-op state back to the plan, and reads
// back what decides a takeover. Such a grant that cannot be put back is a failure, never a hint.
func (e Engine) restoreClientGrants(target string, f Facts, s Snapshot, log func(string), failures *[]string) {
	p := planGrants(s)
	switch {
	case s.FromTV:
		log("这份还原点读自电视（不可信），旧客户端一律不保留守护权限与悬浮窗权限")
	case p.WSSInferred && p.WSS:
		log("这份备份没有记录旧客户端原有权限；安装前它在接管桌面，保留其守护权限")
	case p.WSSInferred || p.OpsInferred:
		log("这份备份没有完整记录旧客户端原有权限；按安装前未接管处理，收回守护权限与悬浮窗等权限")
	}
	names := make([]string, 0, len(p.Ops))
	for op := range p.Ops {
		if !opModes[p.Ops[op]] {
			p.Ops[op] = "default"
		}
		names = append(names, op)
	}
	sort.Strings(names)
	for _, op := range names {
		shell(e.Runner, target, "appops", "set", pkg, op, p.Ops[op])
	}
	// Read back the ops that let the client open itself at boot (Android 10+).
	if f.SDK >= 29 && len(p.Ops) > 0 {
		out, err := query(e.Runner, target, "appops", "get", pkg)
		if err != nil || refusal(out) {
			if !p.OpsInferred {
				*failures = append(*failures, "无法读回旧客户端的悬浮窗权限，不能确认已恢复为安装前状态")
			} else {
				log("提示：本机读不到应用权限（appops）；以重启复查为准")
			}
		} else {
			now := parseAppOps(out)
			for _, op := range names {
				got := now[op]
				if got == "" {
					got = "default"
				}
				if takeoverOps[op] && (got == "allow") != (p.Ops[op] == "allow") {
					*failures = append(*failures, "旧客户端的 "+op+" 现为 "+got+"（安装前 "+p.Ops[op]+"），重启后它可能自己打开")
				}
			}
		}
	}
	dump, derr := query(e.Runner, target, "dumpsys", "package", pkg)
	now, known := wssGranted(dump)
	switch {
	case !p.WSS:
		if derr != nil || !known || now {
			shell(e.Runner, target, "pm", "revoke", pkg, wssPermission)
			dump, derr = query(e.Runner, target, "dumpsys", "package", pkg)
			now, known = wssGranted(dump)
		}
		if derr != nil || !known {
			log("提示：本机读不到权限清单，已执行收回修改系统设置权限但无法读回确认；务必保留“还原后重启复查”")
		} else if now {
			*failures = append(*failures, "旧客户端仍持有修改系统设置权限，重启后会重新打开守护、接管桌面")
		}
	case !p.WSSInferred && derr == nil && known && !now:
		if _, er := shell(e.Runner, target, "pm", "grant", pkg, wssPermission); er != nil {
			*failures = append(*failures, "旧客户端原有的修改系统设置权限未能恢复")
		}
	}
}
