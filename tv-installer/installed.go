package main

import (
	"errors"
	"regexp"
	"strings"
)

// An unknown package may produce no output and exit 1. An independent successful
// user-0 package listing distinguishes that case from a broken/denied query.
func clientPaths(r Runner, target string) ([]string, error) {
	raw, err := shell(r, target, "pm", "path", pkg)
	if strings.TrimSpace(raw) != "" {
		if err != nil {
			return nil, errors.New("读取原客户端APK路径被拒绝或失败；未改动电视")
		}
		paths := []string{}
		seen := map[string]bool{}
		for _, line := range strings.Split(strings.TrimSpace(raw), "\n") {
			line = strings.TrimSpace(line)
			if !strings.HasPrefix(line, "package:") {
				return nil, errors.New("原客户端APK路径响应异常，停止安装")
			}
			path := strings.TrimPrefix(line, "package:")
			if !strings.HasPrefix(path, "/") || !strings.HasSuffix(path, ".apk") || strings.ContainsAny(path, "\r\n\x00") || seen[path] {
				return nil, errors.New("原客户端APK路径异常，停止安装")
			}
			seen[path] = true
			paths = append(paths, path)
		}
		return paths, nil
	}
	list, listErr := shell(r, target, "pm", "list", "packages", "--user", "0")
	if listErr != nil {
		return nil, errors.New("无法核验电视包管理状态；请检查ADB授权或系统服务，未改动电视")
	}
	valid := regexp.MustCompile(`^package:([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)$`)
	count := 0
	found := false
	for _, line := range strings.Split(strings.TrimSpace(list), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		match := valid.FindStringSubmatch(line)
		if match == nil {
			return nil, errors.New("电视包管理清单响应异常，不能判定客户端未安装")
		}
		count++
		found = found || match[1] == pkg
	}
	if count == 0 {
		return nil, errors.New("电视未返回有效包管理清单，不能判定客户端未安装")
	}
	if found {
		return nil, errors.New("电视已登记影院，但无法读取原APK路径；停止覆盖以保留原数据和恢复路径")
	}
	return []string{}, nil
}
