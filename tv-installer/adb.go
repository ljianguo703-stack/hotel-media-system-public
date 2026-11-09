package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Runner interface {
	Run(serial string, args ...string) (string, error)
}
type ADB struct {
	Path string
	Port string
}

// Large enough for a full `dumpsys window` / `dumpsys activity activities` on busy TVs. Truncation
// is never silent: shell commands carry an end marker, and a cut-off reply loses it.
const maxADBOutput = 16 << 20

func (a ADB) Run(serial string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Second)
	defer cancel()
	argv := []string{"-P", a.Port}
	if serial != "" {
		argv = append(argv, "-s", serial)
	}
	argv = append(argv, args...)
	cmd := exec.CommandContext(ctx, a.Path, argv...)
	cmd.Env = append(os.Environ(), "ADB_MDNS_AUTO_CONNECT=0", "ADB_SERVER_SOCKET=tcp:127.0.0.1:"+a.Port)
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out
	err := cmd.Run()
	s := strings.TrimSpace(out.String())
	if len(s) > maxADBOutput {
		s = s[:maxADBOutput]
	}
	if err != nil {
		return s, fmt.Errorf("ADB 执行失败：%w", err)
	}
	return s, nil
}

// rcMarker carries the device-side exit status inside stdout. Before Android 7 (no adb shell_v2)
// `adb shell` always exits 0, and vendor adbd builds differ; the marker is produced by the TV's own
// shell, so the result no longer depends on the transport (quiet-tizen sdb.py:266, cleanyourtv tv.py:509).
const rcMarker = "__HTV_RC="

var errIncompleteReply = errors.New("电视命令输出不完整（没有收到结束标记），可能连接中断或输出被截断")

// shellRC runs one command on the TV and returns its output (CRLF normalised, marker removed) and
// the exit status reported by the TV's shell. A missing marker is always an error.
func shellRC(r Runner, target string, args ...string) (string, int, error) {
	qs := make([]string, len(args))
	for i, s := range args {
		qs[i] = quote(s)
	}
	raw, err := r.Run(target, "shell", strings.Join(qs, " ")+"; echo "+rcMarker+"$?")
	raw = strings.ReplaceAll(raw, "\r\n", "\n")
	raw = strings.ReplaceAll(raw, "\r", "")
	i := strings.LastIndex(raw, rcMarker)
	if i < 0 {
		if err != nil {
			return strings.TrimSpace(raw), -1, err
		}
		return strings.TrimSpace(raw), -1, errIncompleteReply
	}
	digits := raw[i+len(rcMarker):]
	n := 0
	for n < len(digits) && digits[n] >= '0' && digits[n] <= '9' {
		n++
	}
	rc, convErr := strconv.Atoi(digits[:n])
	// With shell_v2, stderr is a separate stream and may arrive after the marker: keep the text on
	// both sides of the marker, drop only the marker itself.
	body := strings.TrimSpace(strings.TrimSpace(raw[:i]) + "\n" + strings.TrimSpace(digits[n:]))
	if convErr != nil {
		return body, -1, errIncompleteReply
	}
	return body, rc, nil
}

// Phrases Android and vendor shells print when a write was refused while still exiting 0 on
// some builds. Matched case-insensitively: Android prints "Permission Denial", older code here
// only looked for "Permission denial" and missed it.
var deviceRefusals = []string{"securityexception", "security exception", "permission denial", "unknown command", "exception occurred while executing", "segmentation fault", ": not found", "can't find service", "failure [", "unknown option", "operation not allowed", "java.lang.", "no shell command implementation"}

func refusal(out string) bool {
	lower := strings.ToLower(out)
	if strings.HasPrefix(lower, "error") || strings.Contains(lower, "\nerror") {
		return true
	}
	for _, p := range deviceRefusals {
		if strings.Contains(lower, p) {
			return true
		}
	}
	return false
}

// shell is for commands that change or must succeed: a non-zero status or a refusal text fails.
func shell(r Runner, target string, args ...string) (string, error) {
	out, rc, err := shellRC(r, target, args...)
	if err != nil {
		return out, err
	}
	if rc != 0 || refusal(out) {
		return out, errors.New("电视拒绝了该操作")
	}
	return out, nil
}

// query is for read-only dumps whose normal text may mention exceptions (dumpsys history lines):
// only the exit status and the end marker decide.
func query(r Runner, target string, args ...string) (string, error) {
	out, rc, err := shellRC(r, target, args...)
	if err != nil {
		return out, err
	}
	if rc != 0 {
		return out, fmt.Errorf("电视命令退出状态 %d", rc)
	}
	return out, nil
}

type ADBConnectionError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Detail  string `json:"detail"`
}

func (e *ADBConnectionError) Error() string {
	if e.Detail != "" {
		return e.Message + "\nADB 返回：" + e.Detail
	}
	return e.Message
}

func connectionFailure(code, message, detail string) error {
	detail = strings.Map(func(r rune) rune {
		if r < 32 && r != '\n' && r != '\t' {
			return -1
		}
		return r
	}, strings.TrimSpace(detail))
	if len(detail) > 1200 {
		detail = detail[:1200]
	}
	return &ADBConnectionError{Code: code, Message: message, Detail: detail}
}

func connect(r Runner, target string) error {
	// adb connect can exit zero even when its reply says "failed to connect".
	// Check the reply before polling: a missing transport is not proof of missing TV consent.
	reply, err := r.Run("", "connect", target)
	lower := strings.ToLower(reply)
	pendingAuth := strings.Contains(lower, "unauthorized") || strings.Contains(lower, "failed to authenticate")
	if !pendingAuth {
		for _, text := range []string{"failed to connect", "cannot connect", "unable to connect", "connection refused", "connection timed out", "no route to host", "network is unreachable", "operation not permitted", "permission denied"} {
			if strings.Contains(lower, text) {
				return connectionFailure("network", "尚未连通电视的调试端口，这不是电视未授权。请核对电视 IP、调试开关与连接端口；另一台电脑还需核对局域网访问权限和网络隔离。", reply)
			}
		}
		if err != nil {
			return connectionFailure("adb", "ADB 连接命令未能执行完成，请先重新检查工具和连接。", strings.TrimSpace(reply+"\n"+err.Error()))
		}
	}
	last := ""
	for i := 0; i < 3; i++ {
		s, e := r.Run(target, "get-state")
		s = strings.TrimSpace(s)
		if e == nil && s == "device" {
			return nil
		}
		last = s
		if last == "" && e != nil {
			last = e.Error()
		}
		if i < 2 {
			time.Sleep(time.Second)
		}
	}
	if pendingAuth || strings.Contains(strings.ToLower(last), "unauthorized") {
		return connectionFailure("unauthorized", "电视正在等待这台电脑的独立授权：请在电视选择“始终允许”，然后重新检测。没有弹窗时可点“重新请求电视授权”；仍无弹窗时在电视关闭再打开调试。", strings.TrimSpace(reply+"\n"+last))
	}
	if strings.Contains(strings.ToLower(last), "offline") {
		return connectionFailure("offline", "电视调试连接处于离线状态：可先重新请求电视授权；仍离线时在电视关闭再打开调试。", strings.TrimSpace(reply+"\n"+last))
	}
	return connectionFailure("transport", "没有取得可用的电视调试连接，当前不能判定为未授权。请按下方 ADB 原因核对 IP、连接端口和网络；使用6位配对码的无线调试需先配对。", strings.TrimSpace(reply+"\n"+last))
}

func reconnectTV(r Runner, target string) error {
	// Only this target on this tool's isolated ADB server. Never kill other servers,
	// revoke TV trust, replace keys, or touch another connected device.
	if out, err := r.Run("", "disconnect", target); err != nil {
		return connectionFailure("adb", "无法断开这台设备的旧连接，请先重新检查工具。", strings.TrimSpace(out+"\n"+err.Error()))
	}
	return connect(r, target)
}

func inspect(r Runner, target string) (Facts, error) {
	out, e := query(r, target, "getprop")
	if e != nil {
		return Facts{}, errors.New("无法读取电视系统信息：" + e.Error())
	}
	m := map[string]string{}
	for _, line := range strings.Split(out, "\n") {
		parts := strings.SplitN(strings.TrimSpace(line), "]: [", 2)
		if len(parts) == 2 {
			m[strings.TrimPrefix(parts[0], "[")] = strings.TrimSuffix(parts[1], "]")
		}
	}
	var sdk int
	fmt.Sscanf(m["ro.build.version.sdk"], "%d", &sdk)
	f := Facts{Brand: m["ro.product.brand"], Model: m["ro.product.model"], Device: m["ro.product.device"], SDK: sdk, Firmware: m["ro.build.version.incremental"], Fingerprint: m["ro.build.fingerprint"], CryptoType: m["ro.crypto.type"], Manufacturer: m["ro.product.manufacturer"]}
	if f.Brand == "" || f.Model == "" || f.SDK == 0 {
		return f, errors.New("没有读到完整 Android 系统信息，暂不支持安装")
	}
	return f, nil
}

var installFailure = regexp.MustCompile(`(INSTALL_(?:FAILED|PARSE_FAILED)_[A-Z0-9_]+)`)

// installAdvice turns the package manager's failure code into what the installer can tell staff.
func installAdvice(out string) (string, string) {
	code := ""
	if m := installFailure.FindStringSubmatch(out); m != nil {
		code = m[1]
	}
	switch {
	case code == "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
		return code, "原客户端签名不同，已停止；不会卸载或清除原数据"
	case strings.HasPrefix(code, "INSTALL_FAILED_VERIFICATION"):
		return code, "电视的安装校验拦截了 ADB 安装（常见于 TCL/雷鸟）"
	case code == "INSTALL_FAILED_INSUFFICIENT_STORAGE":
		return code, "电视存储空间不足，请先清理电视空间再安装"
	case code == "INSTALL_FAILED_OLDER_SDK":
		return code, "电视系统低于客户端最低要求（Android 5.0）"
	case code == "INSTALL_FAILED_NO_MATCHING_ABIS" || code == "INSTALL_FAILED_CPU_ABI_INCOMPATIBLE":
		return code, "客户端与电视处理器架构不匹配"
	case code == "INSTALL_FAILED_USER_RESTRICTED":
		return code, "电视禁止通过调试安装应用：请在开发者选项打开“通过USB安装/USB安装”或在安全设置允许"
	case code == "INSTALL_FAILED_ABORTED":
		return code, "电视上的安装确认被取消或超时；请在电视上点“仍然安装/允许”后重试"
	case code == "INSTALL_FAILED_VERSION_DOWNGRADE":
		return code, "电视已有更高版本的影院客户端，停止以免降级"
	case code == "INSTALL_FAILED_DUPLICATE_PERMISSION" || code == "INSTALL_FAILED_CONFLICTING_PROVIDER":
		return code, "与电视上另一个应用冲突，需人工核对"
	case strings.HasPrefix(code, "INSTALL_PARSE_FAILED"):
		return code, "电视无法解析安装包，请核对便携包完整性"
	case code != "":
		return code, "电视拒绝安装（" + code + "）"
	}
	return "", "APK 安装未返回成功；请查看型号、空间与安装授权，不会强制卸载或降级"
}

// streamingProblem reports adb's streamed-install transport failures; some TVs fail every time and
// need --no-streaming (cleanyourtv-app reports/problem-1130: Sony VH2, TCL C06, Mi Box 3).
func streamingProblem(out string) bool {
	lower := strings.ToLower(out)
	if installFailure.MatchString(out) {
		return false
	}
	return strings.Contains(lower, "streamed install") || strings.Contains(lower, "protocol fault") || strings.Contains(lower, "connection reset") || strings.Contains(lower, "failed to write") || strings.Contains(lower, "broken pipe") || strings.Contains(lower, "closed")
}

// adbInstall installs one APK and returns the package manager reply. A streamed-install transport
// failure is retried once without streaming; any reply other than Success is a failure.
func adbInstall(r Runner, target string, args ...string) (string, error) {
	return adbInstallCmd(r, target, "install", args...)
}
func adbInstallCmd(r Runner, target, sub string, args ...string) (string, error) {
	out, err := r.Run(target, append([]string{sub}, args...)...)
	if (err != nil || !strings.Contains(out, "Success")) && streamingProblem(out) {
		out, err = r.Run(target, append([]string{sub, "--no-streaming"}, args...)...)
	}
	if err != nil || !strings.Contains(out, "Success") || installFailure.MatchString(out) {
		if err == nil {
			err = errors.New("install failed")
		}
		return out, err
	}
	return out, nil
}
