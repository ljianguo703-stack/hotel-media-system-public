package main

import (
	"errors"
	"fmt"
	"net"
	"strconv"
	"strings"
	"time"
)

var errRebootUnreachable = errors.New("重启后无法重新连接电视调试")

// errWrongTV: the address now answers with another device (DHCP lease reused, a box plugged in).
var errWrongTV = errors.New("这个地址连到的不是原来那台电视")

// errRestoreNotRechecked: a first-install TV was restored (client uninstalled, HOME checked before
// the reboot) but its firmware switched ADB off at boot, so the boot screen could not be looked at.
var errRestoreNotRechecked = errors.New("还原已完成：影院已卸载，重启前已确认主页键回到原桌面；这台电视开机时系统会关闭调试，重启后的开机画面请到电视前看一眼")

// errIdentityUnreadable: the identity could not be read, so it is unknown which device answers.
// Kept apart from errWrongTV: a passing glitch must not be reported as an address conflict.
var errIdentityUnreadable = errors.New("无法读取电视身份")

// identityOnce is the digest of the TV's android_id; the raw value never leaves this function.
func (e Engine) identityOnce(target string) (string, bool) {
	id, err := shell(e.Runner, target, "settings", "get", "secure", "android_id")
	id = strings.TrimSpace(id)
	if err != nil || id == "" || id == "null" {
		return "", false
	}
	return digest([]byte(id)), true
}

// otherTV: the identity read at this address is readable and differs (another device answers).
// ok tells whether it could be read at all; an unreadable one proves nothing either way.
func (e Engine) otherTV(target, identity string) (other, ok bool) {
	id, ok := e.identityOnce(target)
	return ok && id != identity, ok
}

// tvIdentity reads the identity, retrying a transient failure (busy settings provider, a dropped
// shell) twice.
func (e Engine) tvIdentity(target string) (string, error) {
	for i := 0; i < 3; i++ {
		if i > 0 {
			e.wait(2 * time.Second)
		}
		if id, ok := e.identityOnce(target); ok {
			return id, nil
		}
	}
	return "", errIdentityUnreadable
}

func (e Engine) bootID(target string) string {
	if out, err := query(e.Runner, target, "cat", "/proc/sys/kernel/random/boot_id"); err == nil {
		return strings.TrimSpace(out)
	}
	return ""
}
func (e Engine) uptime(target string) float64 {
	out, err := query(e.Runner, target, "cat", "/proc/uptime")
	if err != nil {
		return -1
	}
	fields := strings.Fields(out)
	if len(fields) == 0 {
		return -1
	}
	v, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return -1
	}
	return v
}

// mdnsTarget finds the new wireless-debugging port of the same TV after a reboot (Android 11+
// pairing assigns a fresh connect port each time).
func (e Engine) mdnsTarget(host string) string {
	out, err := e.Runner.Run("", "mdns", "services")
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(out, "\n") {
		if !strings.Contains(line, "_adb-tls-connect") {
			continue
		}
		for _, f := range strings.Fields(line) {
			if h, _, err := net.SplitHostPort(f); err == nil && h == host {
				if t, err := validTarget(f); err == nil {
					return t
				}
			}
		}
	}
	return ""
}

// rebootAndReconnect reboots the TV and waits until the same TV is back with a new boot. Settings
// and package state are flushed to disk asynchronously, so it waits before rebooting
// (cleanyourtv TROUBLESHOOTING.md:865-874). Boot is complete only when sys.boot_completed=1 and the
// boot id changed (or, where boot_id is unreadable, uptime is short). With an identity, the device
// is checked right before the reboot is sent and again once it is back: a different device at the
// address is never rebooted, and never taken for the TV (errWrongTV).
func (e Engine) rebootAndReconnect(target, identity string, log func(string)) (string, error) {
	e.wait(15 * time.Second)
	if identity != "" {
		if err := e.sameTV(target, identity); err != nil {
			return target, err
		}
	}
	before := e.bootID(target)
	upBefore := e.uptime(target)
	sent := time.Now()
	e.Runner.Run(target, "reboot")
	log("已发送重启，等待电视重新开机并恢复调试连接（最长约 4 分钟）")
	e.wait(20 * time.Second)
	host, port, _ := net.SplitHostPort(target)
	current := target
	for i := 0; i < 48; i++ {
		if i > 0 {
			e.wait(5 * time.Second)
		}
		if port != "5555" && i%3 == 2 {
			if alt := e.mdnsTarget(host); alt != "" {
				current = alt
			}
		}
		e.Runner.Run("", "connect", current)
		st, err := e.Runner.Run(current, "get-state")
		if err != nil || strings.TrimSpace(st) != "device" {
			continue
		}
		done, derr := query(e.Runner, current, "getprop", "sys.boot_completed")
		if derr != nil || strings.TrimSpace(done) != "1" {
			continue
		}
		// A readable identity that differs is another device, whatever its boot looks like.
		idOK := identity == ""
		if identity != "" {
			other, ok := e.otherTV(current, identity)
			if other {
				return current, errWrongTV
			}
			idOK = ok
		}
		booted := false
		if before != "" {
			now := e.bootID(current)
			booted = now != "" && now != before
		} else if up := e.uptime(current); up >= 0 && upBefore >= 0 && up < upBefore+time.Since(sent).Seconds()-5 {
			// Without boot_id: a real reboot makes uptime smaller than it would be had the TV kept running.
			booted = true
		}
		if !booted || !idOK {
			continue // an unreadable identity right after boot: the settings provider may be starting
		}
		return e.settleAfterBoot(current, identity, log)
	}
	return target, errRebootUnreachable
}

// errADBOffAfterBoot: the TV came back, then debugging went away while it finished starting up and
// did not return — the firmware switches ADB off at boot and nothing switched it back on.
var errADBOffAfterBoot = errors.New("电视开机后系统关闭了 ADB 调试")

// settleMinUptime: by then the vendor's own BOOT_COMPLETED handlers have run.
const settleMinUptime = 100.0

// settleAfterBoot: sys.boot_completed=1 comes before apps receive BOOT_COMPLETED, and some firmware
// switches ADB off in its own BOOT_COMPLETED handler. On MiTV-ASTP0 (OS3.0.104) com.xiaomi.mitv.settings
// set adb_enabled=0 nine seconds after the tool had reconnected, in the middle of the APK install
// (field log 2026-10-30 15:44:52). A channel counts only once it has stayed up for 30 s after the TV
// has been running long enough; a drop that comes back within 30 s (the hotel client putting ADB
// back on) is tolerated.
func (e Engine) settleAfterBoot(current, identity string, log func(string)) (string, error) {
	log("等电视把开机任务做完（约 1–2 分钟），确认调试连接一直在")
	host, port, _ := net.SplitHostPort(current)
	lost, stable, dropped := 0, 0, false
	for i := 0; i < 40; i++ {
		e.wait(5 * time.Second)
		if port != "5555" && lost > 0 && lost%3 == 0 {
			if alt := e.mdnsTarget(host); alt != "" {
				current = alt
			}
		}
		up := false
		if lost > 0 {
			e.Runner.Run("", "connect", current)
		}
		if st, err := e.Runner.Run(current, "get-state"); err == nil && strings.TrimSpace(st) == "device" {
			on, oerr := query(e.Runner, current, "settings", "get", "global", "adb_enabled")
			up = oerr == nil && strings.TrimSpace(on) != "0"
		}
		if !up {
			lost, stable, dropped = lost+1, 0, true
			if lost >= 6 {
				return current, errADBOffAfterBoot
			}
			continue
		}
		lost = 0
		if identity != "" {
			if other, _ := e.otherTV(current, identity); other {
				return current, errWrongTV
			}
		}
		stable++
		uptime := e.uptime(current)
		if stable >= 6 && (uptime >= settleMinUptime || (uptime < 0 && stable >= 12)) {
			if dropped {
				log("开机过程中调试连接断过，已自动恢复")
			}
			return current, nil
		}
	}
	return current, errADBOffAfterBoot
}

// observeStability watches a TV for two minutes after its cold boot. Some firmwares let the guard
// work for a while and then clear it (TCL T966D5 about every 52 s) or kill the app ~30 s later
// (Skyworth Android 13 ANRs) — a single boot check passes on those. Settings are read every 2 s
// (a 10 s period can stay in phase with a 52 s cycle and never see it), binding every 10 s, and the
// decisive test — HOME returns to the client — runs at about 40, 80 and 120 s.
func (e Engine) observeStability(target string, log func(string)) error {
	log("稳定观察 2 分钟：守护是否被系统清除、主页键是否始终回到影院")
	gone := 0
	for i := 1; i <= 60; i++ {
		e.wait(2 * time.Second)
		list, le := shell(e.Runner, target, "settings", "get", "secure", "enabled_accessibility_services")
		on, oe := shell(e.Runner, target, "settings", "get", "secure", "accessibility_enabled")
		if le == nil && oe == nil && (!hasGuard(list) || strings.TrimSpace(on) != "1") {
			gone++
			if gone >= 3 {
				return fmt.Errorf("约 %d 秒时影院守护已被系统关闭超过 4 秒，这台电视的接管不能持续", i*2)
			}
		} else {
			gone = 0
		}
		if i%5 == 0 && guardBinding(e.Runner, target) == "unbound" {
			log(fmt.Sprintf("提示：约 %d 秒时守护未绑定", i*2))
		}
		if i%20 == 0 {
			if err := e.homeKeyReturns(target, 1); err != nil {
				return fmt.Errorf("稳定观察约 %d 秒时%s", i*2, err.Error())
			}
		}
	}
	log("稳定观察通过")
	return nil
}

// guardStaysOff watches a restored TV whose older client stays installed without its guard. A client
// that still can (WRITE_SECURE_SETTINGS) puts the guard back from its BootReceiver, sync adapter or
// listener — the takeover a restore must not leave behind. BOOT_COMPLETED can reach an app well
// after sys.boot_completed on a busy TV, so the watch runs a full minute after the boot settled.
// With watchFront (Android 10+, overlay op unreadable) a client coming to the front on its own during
// the watch fails it too: its late BOOT_COMPLETED can only open it with the overlay op.
func (e Engine) guardStaysOff(target string, s Snapshot, watchFront bool, log func(string)) error {
	log("观察 1 分钟：旧客户端的守护是否会自己重新打开")
	for i := 0; i < 20; i++ {
		e.wait(3 * time.Second)
		if list, err := shell(e.Runner, target, "settings", "get", "secure", "enabled_accessibility_services"); err == nil && hasGuard(list) {
			return errors.New("重启后旧影院客户端重新打开了守护（会再次接管桌面），需人工核对")
		}
		if watchFront {
			if fg, _, err := foreground(e.Runner, target); err == nil && fg == pkg {
				return errClientOpenedItself
			}
		}
	}
	if !planGrants(s).WSS {
		if dump, err := query(e.Runner, target, "dumpsys", "package", pkg); err == nil {
			if now, known := wssGranted(dump); known && now {
				return errors.New("重启后旧客户端仍持有修改系统设置权限，下次开机可能重新接管桌面")
			}
		}
	}
	return nil
}

// sameTV: errIdentityUnreadable when the identity cannot be read, errWrongTV when it differs (or
// there is no recorded identity to compare with).
func (e Engine) sameTV(target, identity string) error {
	if identity == "" {
		return errWrongTV
	}
	id, err := e.tvIdentity(target)
	if err != nil {
		return err
	}
	if id != identity {
		return errWrongTV
	}
	return nil
}

// verifyBoot: after a real cold boot the hotel client must come to the front and stay there, and
// HOME must still return to it. Vendor launchers often show first for 3–30 s (Aerial README table).
func (e Engine) verifyBoot(target, identity string, log func(string)) error {
	if err := e.sameTV(target, identity); err != nil {
		return err
	}
	log("电视已重新开机，等待影院稳定出现在前台")
	if got, err := e.waitForeground(target, pkg, false, 45, 4, 3*time.Second); err != nil {
		return errors.New("软件重启后影院没有稳定出现在前台（最后前台：" + got + "），这台电视的开机接管不成立")
	}
	if guardBinding(e.Runner, target) == "unbound" {
		log("提示：开机后守护尚未绑定，HOME 检查继续以实际前台为准")
	}
	if err := e.homeKeyReturns(target, 1); err != nil {
		return errors.New("软件重启后" + err.Error())
	}
	log("重启验证（软件重启）通过：开机进入影院，HOME 键返回影院；拔电开机与遥控待机唤醒仍需到电视前验收")
	return nil
}

// verifyRestoreAfterReboot cold-boots a restored TV. Many hotel TVs boot into live TV or an input
// rather than the launcher, so at boot it only requires that our client is not in front (unless the
// restored state keeps its guard) and that no chooser shows where there was a default; the HOME key
// then decides. It returns the address the TV came back on.
var errClientOpenedItself = errors.New("重启后旧影院客户端自己打开到了前台，且本机读不到它的悬浮窗权限，无法确认安装前是否也这样（例如电视设置了开机启动应用），需人工核对")

// overlayMode reads the client's overlay op now; false when the TV does not answer.
func (e Engine) overlayMode(target string) (string, bool) {
	out, err := query(e.Runner, target, "appops", "get", pkg)
	if err != nil || refusal(out) {
		return "", false
	}
	if m := parseAppOps(out)["SYSTEM_ALERT_WINDOW"]; m != "" {
		return m, true
	}
	return "default", true
}

func (e Engine) verifyRestoreAfterReboot(target string, f Facts, s Snapshot, log func(string)) (string, error) {
	log("重启电视复查还原结果")
	current, err := e.rebootAndReconnect(target, s.Identity, log)
	if errors.Is(err, errWrongTV) {
		return target, errors.New("还原操作已完成，但重启复查时这个地址连到的不是这台电视（IP 可能被占用），请到电视前确认画面")
	}
	if errors.Is(err, errIdentityUnreadable) {
		return target, errors.New("还原操作已完成，但重启复查时无法确认电视身份，请到电视前确认画面")
	}
	if err != nil {
		if errors.Is(err, errADBOffAfterBoot) && !s.Installed {
			// The client is gone, so nothing can take the TV back; only the boot screen is unchecked.
			return target, errRestoreNotRechecked
		}
		return target, errors.New("还原操作已完成，但重启后无法重新连接电视调试，请到电视前确认画面")
	}
	if err = e.sameTV(current, s.Identity); err != nil {
		return current, err
	}
	if !s.Installed {
		if paths, perr := clientPaths(e.Runner, current); perr != nil || len(paths) > 0 {
			return current, errors.New("重启后仍检测到影院客户端")
		}
	}
	var got string
	var werr error
	clientStays := s.Installed && expectedFront(s) != pkg
	switch {
	case expectedFront(s) == pkg:
		got, werr = e.waitForeground(current, pkg, false, 40, 3, 3*time.Second)
	case clientStays:
		// The older client stays installed with its own boot behaviour: its BootReceiver opens it
		// when its guard is off (always up to Android 9; from 10 only with the overlay op). What
		// decides a takeover is whether the guard comes back and where HOME goes.
		got, werr = e.waitForeground(current, "", true, 40, 3, 3*time.Second)
	default:
		got, werr = e.waitForeground(current, pkg, true, 40, 3, 3*time.Second)
		if werr == nil && got == "android" && s.HomeResolved != "" {
			werr = errors.New("chooser")
		}
	}
	if werr != nil {
		return current, errors.New("重启后开机画面不对（最后前台：" + got + "），需人工核对")
	}
	if clientStays {
		// From Android 10 the old client can open itself at boot only with the overlay op. Where the
		// TV reports its ops that settles it, and a client in front is the TV's own power-on app or
		// resume setting; where it does not, watching the foreground is all there is.
		watchFront := false
		if p := planGrants(s); f.SDK >= 29 && p.Ops != nil && p.Ops["SYSTEM_ALERT_WINDOW"] != "allow" {
			if mode, ok := e.overlayMode(current); !ok {
				watchFront = true
			} else if mode == "allow" {
				return current, errors.New("重启后旧客户端的悬浮窗权限又成了 allow（安装前不是），它开机会自己打开，需人工核对")
			}
		}
		if err = e.guardStaysOff(current, s, watchFront, log); err != nil {
			return current, err
		}
		if got == pkg {
			log("开机时旧客户端在前台（悬浮窗权限已是安装前状态，应是电视自己的开机启动设置）；以守护状态和主页键结果为准")
		}
	}
	var failures []string
	e.confirmHome(current, s, &failures)
	if len(failures) > 0 {
		return current, errors.New("重启后" + strings.Join(failures, "；"))
	}
	switch {
	case clientStays:
		log("重启复查通过：旧客户端的守护保持关闭，主页键回到原桌面")
	default:
		log("重启复查通过：开机不是影院，主页键回到原桌面")
	}
	if expectedFront(s) == pkg {
		log("（这份备份的原状态本就由旧影院客户端守护桌面）")
	}
	return current, nil
}
