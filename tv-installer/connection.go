package main

import (
	"errors"
	"net/url"
	"regexp"
	"strings"
)

var tokenFormat = regexp.MustCompile(`^[A-Za-z0-9_-]{16,256}$`)

// bareAddress is an address typed without its scheme: an IP or dotted host name with an optional
// port, or a single-label host with an explicit port. Anything else is left as typed and reported.
var bareAddress = regexp.MustCompile(`^([A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:[0-9]{1,5})?|[A-Za-z0-9-]+:[0-9]{1,5})/?$`)

// normalizeServer trims a TV service address and adds http:// when only the scheme was left out.
// Staff copy "IP:port" from a browser's address bar; 1.3.2 rejected that with one combined message
// that did not say which field was wrong.
func normalizeServer(raw string) string {
	s := strings.TrimSpace(raw)
	if bareAddress.MatchString(s) {
		s = "http://" + s
	}
	return s
}

// connectionProblem names the field that is wrong and what it should look like. Keep the texts in
// step with connectionProblem() in ui.html, which checks the same before anything is sent.
func connectionProblem(server, token string) error {
	switch {
	case server == "" && token == "":
		return nil
	case server == "":
		return errors.New("填写了设备凭证但没有服务器地址：现在写入连接时两项须一起填写；稍后接入可全部留空")
	case token == "":
		return errors.New("填写了服务器地址但没有设备凭证：现在写入连接时两项须一起填写；稍后接入可全部留空")
	case !validOrigin(server):
		return errors.New("服务器地址格式不对：应写成 http://服务器IP:端口，例如 http://192.168.1.10:18790（电视服务默认端口 18790；18791 是管理后台）")
	case !tokenFormat.MatchString(token):
		return errors.New("设备凭证格式不对：应是后台“客房设备”为这台电视生成的凭证，16–256 位，只含英文字母、数字、_ 或 -；请检查是否多复制了空格、换行或中文符号")
	}
	return nil
}

// adminPortHint: in this project 18791 is the admin backend; the TV talks to the TV service (18790
// by default). Only a hint: a site may have moved its ports.
func adminPortHint(server string) string {
	if u, err := url.Parse(server); err == nil && u.Port() == "18791" {
		return "端口 18791 通常是管理后台，电视要连的是电视服务（默认 18790），请确认"
	}
	return ""
}
