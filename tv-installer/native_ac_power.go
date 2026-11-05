package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const acPowerProfile = "xiaomi-mulan-pstaatv-104-pw01"
const acPowerKey = "factory_power_mode"

type ACPowerSnapshot struct {
	Profile      string `json:"profile"`
	Key          string `json:"key"`
	Before       string `json:"before"`
	HelperSHA256 string `json:"helper_sha256"`
	Attempted    bool   `json:"attempted,omitempty"`
	Readback     string `json:"readback,omitempty"`
}

func verifiedACPower(f Facts) bool {
	return f.Model == "MiTV-ASTP0" && f.Device == "mulan" && f.SDK == 28 &&
		f.Fingerprint == "Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys"
}

func validACSnapshot(b *ACPowerSnapshot) bool {
	return b != nil && b.Profile == acPowerProfile && b.Key == acPowerKey &&
		(b.Before == "direct" || b.Before == "secondary") && regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(b.HelperSHA256)
}

func (e Engine) powerHelper() (string, string, error) {
	path := filepath.Join(e.Root, "helpers", "power-mode.jar")
	var manifest struct {
		Files map[string]string `json:"files"`
	}
	b, err := os.ReadFile(filepath.Join(e.Root, "manifest.json"))
	if err != nil {
		return "", "", err
	}
	if json.Unmarshal(b, &manifest) != nil {
		return "", "", errors.New("通电开机工具清单无效")
	}
	data, err := os.ReadFile(path)
	if err != nil || manifest.Files["helpers/power-mode.jar"] == "" || digest(data) != manifest.Files["helpers/power-mode.jar"] {
		return "", "", errors.New("通电开机helper缺失或校验失败，请使用完整2.0便携包")
	}
	return path, digest(data), nil
}

func (e Engine) powerCommand(target, id, action string, f Facts) (string, error) {
	if !verifiedACPower(f) || !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(id) {
		return "", errors.New("通电开机操作条件不匹配")
	}
	if action != "read" && action != "enable" && action != "restore-secondary" {
		return "", errors.New("通电开机动作无效")
	}
	path, _, err := e.powerHelper()
	if err != nil {
		return "", err
	}
	remote := "/data/local/tmp/hotel-power-mode-" + id + ".jar"
	if _, err = e.Runner.Run(target, "push", path, remote); err != nil {
		return "", err
	}
	defer query(e.Runner, target, "rm", "-f", remote)
	return shell(e.Runner, target, "env", "CLASSPATH="+remote+":/system/framework/xiaomi.hardware.factoryserver-V1.0-java.jar", "app_process", "/system/bin", "PowerModeTool", action)
}

func modeLine(out, key string) (string, error) {
	var got string
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), key+"=") {
			if got != "" {
				return "", errors.New("原厂模式回执重复")
			}
			got = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), key+"="))
		}
	}
	if got != "secondary" && got != "direct" {
		return "", errors.New("原厂模式回执缺失或未知")
	}
	return got, nil
}

func (e Engine) captureACPower(target string, s *Snapshot) error {
	if !verifiedACPower(s.Facts) {
		return nil
	}
	_, hash, err := e.powerHelper()
	if err != nil {
		return err
	}
	out, err := e.powerCommand(target, s.ID, "read", s.Facts)
	if err != nil {
		return err
	}
	before, err := modeLine(out, "MODE_BEFORE")
	if err != nil {
		return err
	}
	s.NativeACPower = &ACPowerSnapshot{Profile: acPowerProfile, Key: acPowerKey, Before: before, HelperSHA256: hash}
	return nil
}

func (e Engine) applyACPower(target string, s *Snapshot, log func(string)) error {
	if !s.ACPowerRequested {
		return nil
	}
	b := s.NativeACPower
	if b == nil {
		log("本型号/固件暂不支持自动配置通电开机，已跳过；影院安装按原流程继续")
		return nil
	}
	if !validACSnapshot(b) || !verifiedACPower(s.Facts) {
		return errors.New("通电开机原值没有完整备份")
	}
	if err := e.sameTV(target, s.Identity); err != nil {
		return err
	}
	out, err := e.powerCommand(target, s.ID, "read", s.Facts)
	if err != nil {
		return err
	}
	before, err := modeLine(out, "MODE_BEFORE")
	if err != nil || before != b.Before {
		return errors.New("通电模式在备份后发生变化，停止写入")
	}
	if before != "direct" {
		b.Attempted = true
		if err = e.saveSnapshot(*s); err != nil {
			return fmt.Errorf("写入前保存通电开机还原点失败：%w", err)
		}
		if err = e.sameTV(target, s.Identity); err != nil {
			return err
		}
		out, err = e.powerCommand(target, s.ID, "enable", s.Facts)
		if err != nil {
			return err
		}
		if after, er := modeLine(out, "MODE_AFTER"); er != nil || after != "direct" {
			return errors.New("原厂通电模式写入未确认")
		}
	}
	out, err = e.powerCommand(target, s.ID, "read", s.Facts)
	if err != nil {
		return err
	}
	if after, er := modeLine(out, "MODE_BEFORE"); er != nil || after != "direct" {
		return errors.New("原厂通电模式独立读回失败")
	}
	b.Readback = "direct"
	if err = e.saveSnapshot(*s); err != nil {
		return err
	}
	log("原厂通电开机已读回direct；原值已备份。仍需现场拔电和待机后拔电验收，不以软件重启代签")
	return nil
}

func (e Engine) restoreACPower(target string, f Facts, s Snapshot, failures *[]string) {
	b := s.NativeACPower
	if b == nil || !b.Attempted {
		return
	}
	if !validACSnapshot(b) || !verifiedACPower(f) {
		*failures = append(*failures, "通电开机项未还原：备份无效或当前固件不匹配")
		return
	}
	if err := e.sameTV(target, s.Identity); err != nil {
		*failures = append(*failures, "通电开机项未还原：设备身份未确认")
		return
	}
	action := "enable"
	if b.Before == "secondary" {
		action = "restore-secondary"
	}
	out, err := e.powerCommand(target, s.ID, action, f)
	if value, er := modeLine(out, "MODE_AFTER"); err != nil || er != nil || value != b.Before {
		*failures = append(*failures, "通电开机原值还原未确认")
		return
	}
	out, err = e.powerCommand(target, s.ID, "read", f)
	if value, er := modeLine(out, "MODE_BEFORE"); err != nil || er != nil || value != b.Before {
		*failures = append(*failures, "通电开机原值独立读回未确认")
	}
}
