package main

import (
	"archive/zip"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
)

func (e Engine) upgradeAllowed(s Snapshot) error {
	if !s.Installed {
		return nil
	}
	var p struct {
		Schema  int      `json:"schema"`
		Allowed []string `json:"allowed_apk_sha256"`
	}
	b, err := os.ReadFile(filepath.Join(e.Root, "payload", "upgrade-policy.json"))
	if err != nil || json.Unmarshal(b, &p) != nil || p.Schema != 1 {
		return errors.New("缺少可覆盖客户端清单，停止升级")
	}
	allowed := map[string]bool{}
	for _, v := range p.Allowed {
		allowed[v] = true
	}
	for _, v := range s.APKHashes {
		if !allowed[v] {
			return errors.New("当前客户端APK不在本包已核验的可覆盖清单，停止以保留可能更新的功能；请用合并最新影院代码后重建的安装包")
		}
	}
	return nil
}

func (e Engine) legacyDefaults(s Snapshot) string {
	for _, name := range s.APKs {
		z, err := zip.OpenReader(filepath.Join(e.State, "backups", s.ID, name))
		if err != nil {
			continue
		}
		var raw []byte
		for _, f := range z.File {
			if f.Name == "assets/hotel-defaults.json" && f.UncompressedSize64 <= 8192 {
				r, er := f.Open()
				if er == nil {
					raw, _ = io.ReadAll(io.LimitReader(r, 8193))
					r.Close()
				}
				break
			}
		}
		z.Close()
		if len(raw) == 0 || len(raw) > 8192 {
			continue
		}
		var v struct {
			Server string `json:"server"`
			Token  string `json:"device_token"`
			Pin    string `json:"staff_pin_sha256"`
		}
		if json.Unmarshal(raw, &v) != nil || !validOrigin(v.Server) || !regexp.MustCompile(`^[A-Za-z0-9_-]{16,256}$`).MatchString(v.Token) || !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(v.Pin) {
			continue
		}
		safe, _ := json.Marshal(v)
		return string(safe)
	}
	return ""
}
