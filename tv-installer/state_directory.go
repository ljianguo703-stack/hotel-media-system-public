package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// Existing stores are used in place. Never merge PINs or rewrite another store when upgrading.
func stateDirectory(root, explicit, envState, configRoot string) (string, error) {
	for _, selected := range []string{explicit, envState} {
		if selected != "" {
			return filepath.Abs(selected)
		}
	}
	legacy := filepath.Join(root, "data")
	if st, err := os.Stat(legacy); err == nil {
		if !st.IsDir() {
			return "", errors.New("旧data不是目录，请保留原文件后检查")
		}
		return filepath.Abs(legacy)
	} else if !os.IsNotExist(err) {
		return "", fmt.Errorf("无法读取原data，停止生成新资料：%w", err)
	}
	// The organized local delivery keeps this private store beside 01_最新版交付.
	// Limit the lookup to the bundle and three parents; do not scan for other hotels' data.
	dir := root
	for i := 0; i < 4; i++ {
		candidate := filepath.Join(dir, "电视安装本店资料", "data")
		if st, err := os.Stat(filepath.Join(candidate, "hotel.json")); err == nil {
			if !st.Mode().IsRegular() {
				return "", errors.New("本店hotel.json不是普通文件，请检查原资料")
			}
			return filepath.Abs(candidate)
		} else if !os.IsNotExist(err) {
			return "", fmt.Errorf("无法读取本店资料，停止生成新资料：%w", err)
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	if configRoot == "" {
		return "", errors.New("无法确定固定本店资料目录，请用--state指定已有data")
	}
	return filepath.Abs(filepath.Join(configRoot, "hotel-tv-installer", "data"))
}
