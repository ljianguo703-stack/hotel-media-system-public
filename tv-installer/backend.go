package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
)

// Backend is the hotel's own admin backend, which this run registers TVs with (1.3.4). The operator
// types the server address and an admin account once; the tool logs in, creates each TV under
// "客房设备", takes the credential the backend issues and writes it to the TV over ADB. Only the
// session cookie and its CSRF token are kept, in this process's memory: the password is used for
// the single login request and never stored, logged or written to disk, and nothing is kept
// across a restart of the tool.
type Backend struct {
	mu     sync.Mutex
	admin  string // admin origin, e.g. http://10.20.9.215:18791
	tv     string // TV service origin taken from the backend, e.g. http://10.20.9.215:18790
	user   string
	hotel  string
	cookie string
	csrf   string
	client *http.Client
	// shown is what Status returns. It has its own lock so the window never waits for a backend
	// request in progress (mu is held for whole HTTP exchanges).
	smu   sync.Mutex
	shown BackendStatus
}

// BackendStatus is what the window shows; it never holds a secret.
type BackendStatus struct {
	LoggedIn bool   `json:"logged_in"`
	Admin    string `json:"admin,omitempty"`
	TV       string `json:"tv,omitempty"`
	User     string `json:"user,omitempty"`
	Hotel    string `json:"hotel,omitempty"`
}

// BackendDevice is one entry of the backend's "客房设备" list.
type BackendDevice struct {
	ID       string
	Room     string
	Enabled  bool
	Archived bool
	Online   bool
	IP       string
	LastSeen int64
}

// Registrar is what the install needs from the backend; tests replace it.
type Registrar interface {
	Ready() error
	TVOrigin() string
	Lookup(id, ip string) (BackendDevice, bool, error)
	// Register returns the credential and the entry as it was before (its room name, and the
	// last_seen that counts as "not yet heard from with the new credential").
	Register(id, room, model string) (token string, entry BackendDevice, created bool, err error)
	Seen(id string) (BackendDevice, error)
	Discard(id string) error
}

var errBackendSession = errors.New("后台登录已失效（超过 12 小时或已在后台退出），请在“本店配置”重新登录后台")

const (
	defaultAdminPort = "18791"
	defaultTVPort    = "18790"
)

var hostChars = regexp.MustCompile(`^[A-Za-z0-9.-]{1,253}$`)

// backendOrigin turns what the operator typed (an IP, IP:port or a full address) into the admin
// origin. The TV service port is accepted and mapped to the admin port: staff often copy the
// address the TVs use.
func backendOrigin(raw string) (string, error) {
	s := strings.TrimSpace(raw)
	if s == "" {
		return "", errors.New("请填写服务器 IP，例如 10.20.9.215")
	}
	if !strings.Contains(s, "://") {
		s = "http://" + s
	}
	u, err := url.Parse(s)
	bad := errors.New("服务器地址格式不对：填服务器 IP 即可，例如 10.20.9.215（也可写 10.20.9.215:18791）")
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" {
		return "", bad
	}
	host, port := u.Hostname(), u.Port()
	if !hostChars.MatchString(host) || strings.HasPrefix(host, "-") || strings.HasPrefix(host, ".") || strings.HasSuffix(host, ".") {
		return "", bad
	}
	if port != "" {
		if n, e := strconv.Atoi(port); e != nil || n < 1 || n > 65535 {
			return "", bad
		}
	}
	if (port == "" && u.Scheme == "http") || port == defaultTVPort {
		port = defaultAdminPort
	}
	if port == "" {
		return u.Scheme + "://" + host, nil
	}
	return u.Scheme + "://" + net.JoinHostPort(host, port), nil
}

func newBackendClient() *http.Client {
	transport := &http.Transport{Proxy: nil, TLSHandshakeTimeout: 5 * time.Second, ResponseHeaderTimeout: 15 * time.Second}
	return &http.Client{Transport: transport, Timeout: 30 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

// backendError reads the backend's own message ({"error": "..."}), which is already in Chinese.
func backendError(status int, raw []byte) error {
	var body struct {
		Error string `json:"error"`
	}
	json.Unmarshal(raw, &body)
	msg := strings.TrimSpace(body.Error)
	if r := []rune(msg); len(r) > 200 {
		msg = string(r[:200])
	}
	if msg == "" {
		msg = fmt.Sprintf("HTTP %d", status)
	}
	return fmt.Errorf("后台返回：%s", msg)
}

// do sends one request. The caller holds b.mu.
func (b *Backend) do(method, path string, body any, out any, limit int64) (int, error) {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return 0, err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, b.admin+path, reader)
	if err != nil {
		return 0, errors.New("后台地址无效")
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if b.cookie != "" {
		req.AddCookie(&http.Cookie{Name: "hotel_session", Value: b.cookie})
	}
	if method == "POST" && b.csrf != "" {
		req.Header.Set("X-CSRF-Token", b.csrf)
	}
	if b.client == nil {
		b.client = newBackendClient()
	}
	resp, err := b.client.Do(req)
	if err != nil {
		return 0, fmt.Errorf("无法连接管理后台 %s：请核对服务器 IP，确认管理后台已启动（默认端口 18791）且这台电脑与服务器在同一局域网", b.admin)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil || int64(len(raw)) > limit {
		return resp.StatusCode, errors.New("后台回复过大或不完整")
	}
	if resp.StatusCode == 401 && path != "/api/admin/login" {
		b.cookie, b.csrf = "", ""
		b.publish()
		return 401, errBackendSession
	}
	if resp.StatusCode != 200 {
		return resp.StatusCode, backendError(resp.StatusCode, raw)
	}
	if out != nil {
		if path == "/api/admin/login" {
			for _, c := range resp.Cookies() {
				if c.Name == "hotel_session" {
					b.cookie = c.Value
				}
			}
		}
		if err = json.Unmarshal(raw, out); err != nil {
			return 200, errors.New("后台回复格式不对：这个地址可能不是本项目的管理后台（默认端口 18791）")
		}
	}
	return 200, nil
}

type backendState struct {
	Endpoints struct {
		TVPort int `json:"tv_port"`
	} `json:"endpoints"`
	Revision json.RawMessage `json:"revision"`
	Settings struct {
		Hotel string `json:"hotel"`
	} `json:"settings"`
	Devices []struct {
		ID       string `json:"id"`
		Room     string `json:"room"`
		Enabled  *bool  `json:"enabled"`
		Archived bool   `json:"_archived"`
		IP       string `json:"ip"`
		LastSeen *int64 `json:"last_seen"`
		Online   bool   `json:"online"`
	} `json:"devices"`
}

func (b *Backend) state() (backendState, error) {
	var st backendState
	_, err := b.do("GET", "/api/admin/state", nil, &st, 64<<20)
	if err == nil && len(st.Revision) == 0 {
		err = errors.New("后台回复缺少数据版本：这个地址可能不是本项目的管理后台")
	}
	return st, err
}

func (st backendState) device(id string) (BackendDevice, bool) {
	for _, d := range st.Devices {
		if d.ID == id {
			return toDevice(d.ID, d.Room, d.Enabled, d.Archived, d.Online, d.IP, d.LastSeen), true
		}
	}
	return BackendDevice{}, false
}

func toDevice(id, room string, enabled *bool, archived, online bool, ip string, seen *int64) BackendDevice {
	d := BackendDevice{ID: id, Room: room, Enabled: enabled == nil || *enabled, Archived: archived, Online: online, IP: ip}
	if seen != nil {
		d.LastSeen = *seen
	}
	return d
}

// Login checks the account against the backend. The password is not kept.
func (b *Backend) Login(server, username, password string) (BackendStatus, error) {
	admin, err := backendOrigin(server)
	if err != nil {
		return BackendStatus{}, err
	}
	username = strings.TrimSpace(username)
	if username == "" || password == "" {
		return BackendStatus{}, errors.New("请填写后台管理员账号和密码")
	}
	if len(username) > 80 || len(password) > 512 {
		return BackendStatus{}, errors.New("账号或密码过长")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.cookie != "" {
		b.do("POST", "/api/admin/logout", map[string]any{}, nil, 1<<16)
	}
	b.reset()
	b.admin = admin
	var login struct {
		User *struct {
			Name string `json:"name"`
			Role string `json:"role"`
			CSRF string `json:"csrf"`
		} `json:"user"`
	}
	status, err := b.do("POST", "/api/admin/login", map[string]string{"username": username, "password": password}, &login, 1<<16)
	if err != nil {
		b.cookie = ""
		if status == 404 || status == 405 {
			return BackendStatus{}, fmt.Errorf("%s 不是管理后台（%v）：管理后台默认端口 18791", admin, err)
		}
		return BackendStatus{}, err
	}
	if login.User == nil || b.cookie == "" || login.User.CSRF == "" {
		b.cookie = ""
		return BackendStatus{}, errors.New("后台没有返回登录会话：这个地址可能不是本项目的管理后台（默认端口 18791）")
	}
	b.csrf, b.user = login.User.CSRF, login.User.Name
	if login.User.Role != "admin" {
		b.do("POST", "/api/admin/logout", map[string]any{}, nil, 1<<16)
		b.reset()
		return BackendStatus{}, fmt.Errorf("账号 %s 的角色是 %s：登记电视、取得设备凭证需要管理员（admin）账号", login.User.Name, login.User.Role)
	}
	st, err := b.state()
	if err != nil {
		b.do("POST", "/api/admin/logout", map[string]any{}, nil, 1<<16)
		b.reset()
		return BackendStatus{}, err
	}
	u, _ := url.Parse(admin)
	port := defaultTVPort
	if st.Endpoints.TVPort > 0 && st.Endpoints.TVPort < 65536 {
		port = strconv.Itoa(st.Endpoints.TVPort)
	}
	b.tv = "http://" + net.JoinHostPort(u.Hostname(), port)
	b.hotel = st.Settings.Hotel
	b.publish()
	return b.status(), nil
}

func (b *Backend) status() BackendStatus {
	if b.cookie == "" {
		return BackendStatus{}
	}
	return BackendStatus{LoggedIn: true, Admin: b.admin, TV: b.tv, User: b.user, Hotel: b.hotel}
}

// publish updates what Status shows. The caller holds b.mu.
func (b *Backend) publish() {
	st := b.status()
	b.smu.Lock()
	b.shown = st
	b.smu.Unlock()
}

func (b *Backend) Status() BackendStatus {
	b.smu.Lock()
	defer b.smu.Unlock()
	return b.shown
}

func (b *Backend) Logout() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.cookie != "" {
		b.do("POST", "/api/admin/logout", map[string]any{}, nil, 1<<16)
	}
	b.reset()
}

// reset forgets the session. The caller holds b.mu.
func (b *Backend) reset() {
	b.admin, b.tv, b.user, b.hotel, b.cookie, b.csrf = "", "", "", "", "", ""
	b.publish()
}

// Ready asks the backend whether the session still holds (it ends after 12 hours or when the
// account logs out elsewhere), before anything is changed on a TV.
func (b *Backend) Ready() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.cookie == "" {
		return errors.New("还没有登录后台：请在“本店配置”填写服务器 IP 和管理员账号后点“登录后台”")
	}
	var s struct {
		User *struct {
			Role string `json:"role"`
		} `json:"user"`
	}
	if _, err := b.do("GET", "/api/admin/session", nil, &s, 1<<16); err != nil {
		return err
	}
	if s.User == nil {
		b.cookie, b.csrf = "", ""
		b.publish()
		return errBackendSession
	}
	if s.User.Role != "admin" {
		return errors.New("后台账号已不是管理员，不能登记电视")
	}
	return nil
}

func (b *Backend) TVOrigin() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.tv
}

// Lookup finds the entry this TV is using right now: the backend must be hearing from it at this
// TV's address (the client was just restarted, so a TV connected here has just called in). An entry
// that merely exists is not enough: after a restore the TV may be back on another server.
func (b *Backend) Lookup(id, ip string) (BackendDevice, bool, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	st, err := b.state()
	if err != nil {
		return BackendDevice{}, false, err
	}
	if d, ok := st.device(id); ok && !d.Archived && d.Enabled && d.Online && ip != "" && d.IP == ip {
		return d, true, nil
	}
	// Several entries can share an address over time (DHCP); the one heard from last is this TV.
	var best BackendDevice
	found := false
	for _, d := range st.Devices {
		if ip != "" && d.IP == ip && d.Online && !d.Archived && (d.Enabled == nil || *d.Enabled) {
			if c := toDevice(d.ID, d.Room, d.Enabled, d.Archived, d.Online, d.IP, d.LastSeen); !found || c.LastSeen > best.LastSeen {
				best, found = c, true
			}
		}
	}
	return best, found, nil
}

// Register creates the TV under "客房设备" and returns the credential the backend issued. A TV this
// tool registered before gets a new credential instead (the backend shows a credential only once);
// its room name and group are left as staff set them.
func (b *Backend) Register(id, room, model string) (string, BackendDevice, bool, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	for attempt := 0; ; attempt++ {
		st, err := b.state()
		if err != nil {
			return "", BackendDevice{}, false, err
		}
		entry, existed := st.device(id)
		if !existed {
			entry = BackendDevice{ID: id, Room: room, Enabled: true}
		}
		token, created, status, err := b.registerOnce(st, id, room, model)
		if status == 409 && attempt == 0 {
			continue // another change landed in between; read the new revision once
		}
		if err == nil && !tokenFormat.MatchString(token) {
			err = errors.New("后台返回的设备凭证格式不对，没有写入电视")
		}
		return token, entry, created, err
	}
}

func (b *Backend) registerOnce(st backendState, id, room, model string) (string, bool, int, error) {
	if d, ok := st.device(id); ok {
		if d.Archived {
			return "", false, 0, fmt.Errorf("这台电视在后台回收站里（设备 %s，%s）：请先在后台“客房设备”的回收站恢复它，再安装", id, d.Room)
		}
		if !d.Enabled {
			return "", false, 0, fmt.Errorf("这台电视在后台已停用（设备 %s，%s）：请先在后台“客房设备”启用它，再安装", id, d.Room)
		}
		var out struct {
			Credentials []struct {
				ID    string `json:"id"`
				Token string `json:"token"`
			} `json:"credentials"`
			Failed []struct {
				Reason string `json:"reason"`
			} `json:"failed"`
		}
		status, err := b.do("POST", "/api/admin/operations", map[string]any{"kind": "device", "action": "devices_batch", "operation": "rotate", "ids": []string{id}, "confirm": true, "revision": st.Revision}, &out, 1<<20)
		if err != nil {
			return "", false, status, err
		}
		if len(out.Failed) > 0 {
			return "", false, 0, errors.New("后台没能为这台电视换发凭证：" + out.Failed[0].Reason)
		}
		for _, c := range out.Credentials {
			if c.ID == id {
				return c.Token, false, 200, nil
			}
		}
		return "", false, 0, errors.New("后台换发凭证后没有返回新凭证")
	}
	var out struct {
		ID    string  `json:"id"`
		Token *string `json:"token"`
	}
	body := map[string]any{"room": room, "group": "全部客房", "enabled": true, "model": model, "version": "", "revision": st.Revision}
	status, err := b.do("POST", "/api/admin/devices/"+url.PathEscape(id), body, &out, 1<<16)
	if err != nil {
		return "", false, status, err
	}
	if out.Token == nil {
		return "", false, 0, errors.New("后台没有返回新设备的凭证（这台电视可能刚被别人登记过），请重试")
	}
	return *out.Token, true, 200, nil
}

// Seen reads when the backend last heard from this device (TV requests only; the tool's own check
// is sent as a probe and does not count).
func (b *Backend) Seen(id string) (BackendDevice, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	var out struct {
		Device struct {
			ID       string `json:"id"`
			Room     string `json:"room"`
			Enabled  *bool  `json:"enabled"`
			Archived bool   `json:"_archived"`
		} `json:"device"`
		Connection struct {
			LastSeen *int64 `json:"last_seen"`
			IP       string `json:"ip"`
		} `json:"connection"`
		Online bool `json:"online"`
	}
	if _, err := b.do("GET", "/api/admin/operations?section=device&id="+url.QueryEscape(id), nil, &out, 1<<20); err != nil {
		return BackendDevice{}, err
	}
	d := out.Device
	return toDevice(d.ID, d.Room, d.Enabled, d.Archived, out.Online, out.Connection.IP, out.Connection.LastSeen), nil
}

// Discard removes an entry this run created and the TV never kept (the install was rolled back):
// into the recycle bin, then cleared from it, so a retry starts clean. When clearing is refused the
// entry stays in the recycle bin.
func (b *Backend) Discard(id string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if err := b.operation(map[string]any{"kind": "device", "action": "devices_batch", "operation": "archive", "ids": []string{id}}); err != nil {
		return err
	}
	if err := b.operation(map[string]any{"kind": "device", "action": "purge", "ids": []string{id}, "confirm": true}); err != nil {
		return fmt.Errorf("已移入后台回收站，但未能清空：%v", err)
	}
	return nil
}

// archive moves an entry to the recycle bin (tests use it to stand for staff doing so).
func (b *Backend) archive(id string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.operation(map[string]any{"kind": "device", "action": "devices_batch", "operation": "archive", "ids": []string{id}})
}

// operation posts one operations request with the current revision, retrying once when another
// change landed in between. The caller holds b.mu.
func (b *Backend) operation(body map[string]any) error {
	for attempt := 0; ; attempt++ {
		st, err := b.state()
		if err != nil {
			return err
		}
		body["revision"] = st.Revision
		var out struct {
			Failed []struct {
				Reason string `json:"reason"`
			} `json:"failed"`
		}
		status, err := b.do("POST", "/api/admin/operations", body, &out, 1<<20)
		if status == 409 && attempt == 0 {
			continue
		}
		if err == nil && len(out.Failed) > 0 {
			err = errors.New(out.Failed[0].Reason)
		}
		return err
	}
}

// loopbackServer: the address is written into every TV, which cannot reach a server named
// 127.0.0.1 or localhost (that would be the TV itself).
func loopbackServer(raw string) bool {
	admin, err := backendOrigin(raw)
	if err != nil {
		return false
	}
	u, err := url.Parse(admin)
	if err != nil {
		return false
	}
	h := strings.ToLower(u.Hostname())
	if h == "localhost" || strings.HasSuffix(h, ".localhost") {
		return true
	}
	ip := net.ParseIP(h)
	return ip != nil && (ip.IsLoopback() || ip.IsUnspecified())
}

// backendDeviceID is the id this tool gives a TV in the backend: stable for the TV (derived from
// its identity, so a re-install finds the same entry), and revealing nothing of the TV itself.
func backendDeviceID(identity string) string {
	return "tv-" + digest([]byte("hotel-tv-backend-device:" + identity))[:16]
}

// defaultRoom names a TV by its address until staff rename it in the backend.
func defaultRoom(target string) string {
	host, _, err := net.SplitHostPort(target)
	if err != nil {
		host = target
	}
	parts := strings.FieldsFunc(host, func(r rune) bool { return r == '.' || r == ':' })
	if len(parts) == 0 {
		return "待命名"
	}
	return "待命名-" + parts[len(parts)-1]
}

// roomProblem checks a room name typed in the window (the backend allows up to 80 characters).
func roomProblem(room string) error {
	if len([]rune(room)) > 80 {
		return errors.New("房间名最多 80 个字")
	}
	for _, r := range room {
		if unicode.IsControl(r) || r == ' ' || r == ' ' {
			return errors.New("房间名含有换行或控制字符")
		}
	}
	return nil
}
