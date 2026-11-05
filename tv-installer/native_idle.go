package main

import (
	"fmt"
	"strings"
)

// IN14: two native switches, only on the exact TV/firmware tested with real standby/wake.
// No display timeout, power action, native package, HOME or ADB setting is changed here.
var nativeIdleKeys = []struct{ space, name, value string }{
	{"system", "screen_saver_timeout", "-1"},
	{"secure", "screensaver_enabled", "0"},
}

func verifiedNativeIdle(f Facts) bool {
	return f.Model == "MiTV-ASTP0" && f.Device == "mulan" && f.SDK == 28 &&
		f.Fingerprint == "Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys"
}

func (e Engine) applyNativeIdle(target string, s Snapshot, log func(string)) error {
	if !verifiedNativeIdle(s.Facts) {
		return nil
	}
	for _, key := range nativeIdleKeys {
		if _, ok := s.NativeIdle[key.space+"/"+key.name]; !ok {
			return fmt.Errorf("自动屏保原值未备份，停止配置")
		}
		if _, err := shell(e.Runner, target, "settings", "put", key.space, key.name, key.value); err != nil {
			return fmt.Errorf("关闭原生自动屏保失败：%s", key.name)
		}
		out, err := shell(e.Runner, target, "settings", "get", key.space, key.name)
		if err != nil || strings.TrimSpace(out) != key.value {
			return fmt.Errorf("自动屏保配置读回不一致：%s", key.name)
		}
	}
	log("已按本固件实机记录关闭全机自动屏保；手动电源待机保留，原值已写入还原点")
	return nil
}

func (e Engine) restoreNativeIdle(target string, s Snapshot, failures *[]string) {
	for _, key := range nativeIdleKeys {
		value, ok := s.NativeIdle[key.space+"/"+key.name]
		if !ok {
			continue
		} // Old snapshots never invent a previous value.
		args := []string{"settings", "put", key.space, key.name, value}
		if value == "null" || value == "" {
			args = []string{"settings", "delete", key.space, key.name}
		}
		_, err := shell(e.Runner, target, args...)
		out, readErr := shell(e.Runner, target, "settings", "get", key.space, key.name)
		want := value
		if want == "" {
			want = "null"
		}
		if err != nil || readErr != nil || strings.TrimSpace(out) != want {
			*failures = append(*failures, "自动屏保原值还原未确认："+key.name)
		}
	}
}
