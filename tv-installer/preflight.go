package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type ServerCheck struct {
	Items   int    `json:"items"`
	Message string `json:"message"`
}

// Use the TV's actual read-only endpoint. Never forward its credential to redirects or proxies.
func checkServer(origin, token string) (ServerCheck, error) {
	origin, token = normalizeServer(origin), strings.TrimSpace(token)
	if origin == "" && token == "" {
		return ServerCheck{}, errors.New("请输入电视服务地址和这台电视的独立设备凭证")
	}
	if err := connectionProblem(origin, token); err != nil {
		return ServerCheck{}, err
	}
	hint := ""
	if h := adminPortHint(origin); h != "" {
		hint = "；" + h
	}
	req, err := http.NewRequest("GET", strings.TrimRight(origin, "/")+"/api/v1/catalog", nil)
	if err != nil {
		return ServerCheck{}, errors.New("服务器地址无效")
	}
	req.Header.Set("X-TV-Token", token)
	// A probe: the backend does not count it as the TV being online (only the TV's own requests do).
	req.Header.Set("X-TV-Probe", "1")
	transport := &http.Transport{Proxy: nil, TLSHandshakeTimeout: 5 * time.Second, ResponseHeaderTimeout: 6 * time.Second}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 8 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(req)
	if err != nil {
		return ServerCheck{}, errors.New("无法访问电视服务，请检查局域网地址、18790端口、防火墙和HTTPS证书" + hint)
	}
	defer response.Body.Close()
	if response.StatusCode == 401 || response.StatusCode == 403 {
		return ServerCheck{}, errors.New("设备凭证被拒绝，请在后台检查该台电视已启用并复制正确凭证")
	}
	if response.StatusCode != 200 {
		return ServerCheck{}, fmt.Errorf("电视服务返回HTTP %d，请使用电视接口地址而非管理后台或跳转地址%s", response.StatusCode, hint)
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, (4<<20)+1))
	var catalog struct {
		Items *[]json.RawMessage `json:"items"`
	}
	if err != nil || len(raw) > 4<<20 || json.Unmarshal(raw, &catalog) != nil || catalog.Items == nil {
		return ServerCheck{}, errors.New("地址没有返回影院片库，请核对电视服务端口；未更改电视" + hint)
	}
	return ServerCheck{Items: len(*catalog.Items), Message: "电视服务与本台设备凭证验证通过（电脑侧）；电视侧网络与播放仍需安装后验收"}, nil
}

func (e Engine) preflightServer(origin, token string) (ServerCheck, error) {
	origin, token = normalizeServer(origin), strings.TrimSpace(token)
	if e.CheckServer != nil {
		if err := connectionProblem(origin, token); err != nil || origin == "" {
			if err == nil {
				err = errors.New("请输入电视服务地址和这台电视的独立设备凭证")
			}
			return ServerCheck{}, err
		}
		return e.CheckServer(origin, token)
	}
	return checkServer(origin, token)
}

func runtimeCheck(r Runner) (string, error) {
	out, err := r.Run("", "version")
	if err != nil || !strings.Contains(out, "Android Debug Bridge version") {
		return "", errors.New("随包ADB无法运行，请完整解压对应电脑系统的便携包；不要只复制主程序")
	}
	return strings.SplitN(out, "\n", 3)[0] + " · " + strings.SplitN(out+"\n", "\n", 3)[1], nil
}
