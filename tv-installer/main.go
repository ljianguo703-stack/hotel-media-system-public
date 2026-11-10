package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

//go:embed ui.html connection-guides.json source/community-index.json
var assets embed.FS

type Job struct {
	ID      string   `json:"id"`
	State   string   `json:"state"`
	Lines   []string `json:"lines"`
	Results []Result `json:"results"`
	// At: when the job started (1.3.3; older reports fall back to the report file's time).
	At string `json:"at,omitempty"`
	// Restored: loaded from data/reports at start-up — an earlier run, not something happening now.
	Restored bool `json:"restored,omitempty"`
}
type Result struct {
	ACPower         string `json:"ac_power,omitempty"`
	Target          string `json:"target"`
	State           string `json:"state"`
	Message         string `json:"message"`
	Backup          string `json:"backup"`
	ADBClosed       bool   `json:"adb_closed"`
	ConnectionState string `json:"connection_state,omitempty"`
	Tier            string `json:"tier,omitempty"`
	RebootVerified  bool   `json:"reboot_verified,omitempty"`
	// 1.3.4: the backend entry the TV was connected to ("客房设备" room name) and whether the backend
	// saw the TV online.
	Room         string `json:"room,omitempty"`
	ServerOnline bool   `json:"server_online,omitempty"`
	ManagedReady bool   `json:"managed_ready,omitempty"`
}
type App struct {
	Engine  Engine
	Token   string
	Host    string
	Pairing *BrowserPairing
	mu      sync.Mutex
	op      sync.Mutex
	job     *Job
	hotel   Hotel
	// backend: the admin session for registering TVs, in memory only (see backend.go).
	backend *Backend
	// allowLoopbackBackend: browser test fixture only (its fake backend listens on 127.0.0.1).
	allowLoopbackBackend bool
}

func randomID() (string, error) {
	b := make([]byte, 16)
	_, e := rand.Read(b)
	return hex.EncodeToString(b), e
}
func reply(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	json.NewEncoder(w).Encode(v)
}
func fail(w http.ResponseWriter, err error) {
	w.WriteHeader(400)
	var connection *ADBConnectionError
	if errors.As(err, &connection) {
		reply(w, map[string]any{"error": err.Error(), "connection": connection})
		return
	}
	reply(w, map[string]string{"error": err.Error()})
}
func decode(w http.ResponseWriter, r *http.Request, v any) error {
	if r.Method != "POST" {
		return errors.New("此操作仅接受 POST")
	}
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	d.DisallowUnknownFields()
	if e := d.Decode(v); e != nil {
		return errors.New("请求格式无效")
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return errors.New("请求含多余内容")
	}
	return nil
}
func (a *App) handler() http.Handler {
	if a.backend == nil {
		a.backend = &Backend{}
		if a.Engine.Backend == nil {
			a.Engine.Backend = a.backend
		}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
		if r.Host != a.Host {
			http.Error(w, "Host rejected", 403)
			return
		}
		if a.Pairing != nil {
			host, _, err := net.SplitHostPort(r.RemoteAddr)
			ip := net.ParseIP(host)
			if err != nil || ip == nil || (!ip.IsPrivate() && !ip.IsLoopback()) {
				http.Error(w, "Local network only", http.StatusForbidden)
				return
			}
		}
		if r.URL.Path == "/favicon.ico" {
			w.WriteHeader(204)
			return
		}
		if r.URL.Path == "/" {
			if r.Method != "GET" {
				http.Error(w, "Method", 405)
				return
			}
			b, _ := assets.ReadFile("ui.html")
			if a.Pairing != nil {
				b = []byte(strings.Replace(string(b), `name="installer-lan" content="false"`, `name="installer-lan" content="true"`, 1))
			}
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Write(b)
			return
		}
		if !strings.HasPrefix(r.URL.Path, "/api/") {
			http.NotFound(w, r)
			return
		}
		if o := r.Header.Get("Origin"); o != "" && o != "http://"+a.Host {
			http.Error(w, "Origin rejected", 403)
			return
		}
		if r.URL.Path == "/api/pair-browser" && a.Pairing != nil {
			var request struct {
				Code string `json:"code"`
			}
			if err := decode(w, r, &request); err != nil {
				fail(w, err)
				return
			}
			if err := a.Pairing.verify(request.Code); err != nil {
				fail(w, err)
				return
			}
			reply(w, map[string]string{"token": a.Token})
			return
		}
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-App-Token")), []byte(a.Token)) != 1 {
			http.Error(w, "Unauthorized", 401)
			return
		}
		switch r.URL.Path {
		case "/api/connection-guides", "/api/community-index":
			name := "connection-guides.json"
			if r.URL.Path == "/api/community-index" {
				name = "source/community-index.json"
			}
			b, err := assets.ReadFile(name)
			if err != nil || !json.Valid(b) {
				fail(w, errors.New("随包连接资料损坏，请重新解压完整包"))
				return
			}
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
			w.Write(b)
		case "/api/state":
			a.mu.Lock()
			defer a.mu.Unlock()
			reply(w, map[string]any{"version": version, "catalog": a.Engine.Catalog, "has_pin": a.hotel.PinHash != "", "job": a.job, "state_dir": a.Engine.State, "host": runtime.GOOS + "/" + runtime.GOARCH, "attachments": a.Engine.attachments(), "backend": a.backendStatus()})
		case "/api/backend-login":
			var request struct {
				Server   string `json:"server"`
				Username string `json:"username"`
				Password string `json:"password"`
			}
			if e := decode(w, r, &request); e != nil {
				fail(w, e)
				return
			}
			if a.busy() {
				fail(w, errors.New("安装任务进行中，结束后再登录或切换后台"))
				return
			}
			if loopbackServer(request.Server) && !a.allowLoopbackBackend {
				fail(w, errors.New("服务器 IP 请填局域网地址（例如 10.20.9.215），不能用 127.0.0.1 或 localhost：这个地址会写进电视，电视要靠它找到服务器"))
				return
			}
			// The password goes to the backend in this one request and is not kept anywhere.
			st, e := a.backend.Login(request.Server, request.Username, request.Password)
			request.Password = ""
			if e != nil {
				fail(w, e)
				return
			}
			reply(w, st)
		case "/api/backend-logout":
			var request struct{}
			if e := decode(w, r, &request); e != nil {
				fail(w, e)
				return
			}
			if a.busy() {
				fail(w, errors.New("安装任务进行中，结束后再退出后台"))
				return
			}
			a.backend.Logout()
			reply(w, a.backend.Status())
		case "/api/doctor":
			var request struct{}
			if e := decode(w, r, &request); e != nil {
				fail(w, e)
				return
			}
			adb, e := runtimeCheck(a.Engine.Runner)
			if e != nil {
				fail(w, e)
				return
			}
			reply(w, map[string]any{"message": "便携包校验已在启动完成；随包ADB可运行，不需要Python/Java/Node/Android Studio", "adb": adb})
		case "/api/server-check":
			var request struct {
				Server string `json:"server"`
				Token  string `json:"token"`
			}
			if e := decode(w, r, &request); e != nil {
				fail(w, e)
				return
			}
			check, e := a.Engine.preflightServer(request.Server, request.Token)
			if e != nil {
				fail(w, e)
				return
			}
			reply(w, check)
		case "/api/attachment":
			if e := a.Engine.attachment(w, r); e != nil {
				fail(w, e)
			}
		case "/api/pin":
			var request struct {
				Pin string `json:"pin"`
			}
			if e := decode(w, r, &request); e != nil {
				fail(w, e)
				return
			}
			a.mu.Lock()
			defer a.mu.Unlock()
			if a.job != nil && a.job.State == "running" {
				fail(w, errors.New("安装期间不能更换本店 PIN"))
				return
			}
			pin, hash, e := generatePin()
			if e != nil {
				fail(w, e)
				return
			}
			if request.Pin != "" {
				if !regexp.MustCompile(`^[0-9]{6}$`).MatchString(request.Pin) {
					fail(w, errors.New("已有员工PIN必须为6位数字"))
					return
				}
				pin = request.Pin
				sum := sha256.Sum256([]byte(pin))
				hash = hex.EncodeToString(sum[:])
			}
			hotel := Hotel{PinHash: hash}
			if e = atomicJSON(filepath.Join(a.Engine.State, "hotel.json"), hotel); e != nil {
				fail(w, e)
				return
			}
			a.hotel = hotel
			reply(w, map[string]string{"pin": pin, "message": "请保存。仅后续安装或更新的电视应用此 PIN；现有电视不会自动变化。"})
		case "/api/discover":
			if !a.op.TryLock() {
				fail(w, errors.New("已有设备操作运行"))
				return
			}
			defer a.op.Unlock()
			var req struct{}
			if e := decode(w, r, &req); e != nil {
				fail(w, e)
				return
			}
			raw, e := a.Engine.Runner.Run("", "devices", "-l")
			if e != nil {
				fail(w, e)
				return
			}
			mdns, _ := a.Engine.Runner.Run("", "mdns", "services")
			targets := []string{}
			seen := map[string]bool{}
			for _, line := range strings.Split(raw+"\n"+mdns, "\n") {
				for _, s := range strings.Fields(line) {
					if strings.Contains(s, ":") {
						if t, e := validTarget(s); e == nil && !seen[t] {
							targets = append(targets, t)
							seen[t] = true
						}
					}
				}
			}
			reply(w, map[string]any{"targets": targets, "message": "仅列已连接设备和 mDNS 广播；未出现时直接填写电视 IP。"})
		case "/api/reconnect":
			if !a.op.TryLock() {
				fail(w, errors.New("已有设备操作运行"))
				return
			}
			defer a.op.Unlock()
			var req struct {
				Target string `json:"target"`
			}
			if err := decode(w, r, &req); err != nil {
				fail(w, err)
				return
			}
			if a.busy() {
				fail(w, errors.New("请等待当前任务完成"))
				return
			}
			target, err := validTarget(req.Target)
			if err != nil {
				fail(w, err)
				return
			}
			if err = reconnectTV(a.Engine.Runner, target); err != nil {
				fail(w, err)
				return
			}
			reply(w, map[string]string{"message": "这台电脑已取得电视调试连接，请点检测设备继续；未执行安装或恢复。"})
		case "/api/probe":
			if !a.op.TryLock() {
				fail(w, errors.New("已有设备操作运行"))
				return
			}
			defer a.op.Unlock()
			var req struct {
				Target string `json:"target"`
			}
			if e := decode(w, r, &req); e != nil {
				fail(w, e)
				return
			}
			if a.busy() {
				fail(w, errors.New("请等待当前任务完成"))
				return
			}
			f, d, e := a.Engine.detect(req.Target)
			if e != nil {
				fail(w, e)
				return
			}
			var installed *bool
			if d.Allowed || d.Trial {
				target, _ := validTarget(req.Target)
				paths, err := clientPaths(a.Engine.Runner, target)
				if err != nil {
					fail(w, err)
					return
				}
				present := len(paths) > 0
				installed = &present
			}
			ok, failed := a.Engine.fieldHistory(f)
			reply(w, map[string]any{"facts": f, "decision": d, "client_installed": installed, "history": map[string]int{"installed_reboot_verified": ok, "failed": failed}})
		case "/api/diagnose":
			if !a.op.TryLock() {
				fail(w, errors.New("已有设备操作运行"))
				return
			}
			defer a.op.Unlock()
			var req struct {
				Target string `json:"target"`
			}
			if e := decode(w, r, &req); e != nil {
				fail(w, e)
				return
			}
			if a.busy() {
				fail(w, errors.New("请等待当前任务完成"))
				return
			}
			d, e := a.Engine.diagnose(req.Target)
			if e != nil {
				fail(w, e)
				return
			}
			name := regexp.MustCompile(`[^A-Za-z0-9_.-]+`).ReplaceAllString(d.Facts.Brand+"-"+d.Facts.Model+"-sdk"+fmt.Sprint(d.Facts.SDK), "_")
			atomicJSON(filepath.Join(a.Engine.State, "diagnostics", name+"-"+time.Now().Format("20060102-150405")+".json"), d)
			reply(w, d)
		case "/api/restore-from-tv":
			if !a.op.TryLock() {
				fail(w, errors.New("已有设备操作运行"))
				return
			}
			defer a.op.Unlock()
			var req struct {
				Target string `json:"target"`
			}
			if e := decode(w, r, &req); e != nil {
				fail(w, e)
				return
			}
			if a.busy() {
				fail(w, errors.New("请等待当前任务完成"))
				return
			}
			s, e := a.Engine.importTVRestorePoint(req.Target)
			if e != nil {
				fail(w, e)
				return
			}
			reply(w, s)
		case "/api/pair":
			if !a.op.TryLock() {
				fail(w, errors.New("已有设备操作运行"))
				return
			}
			defer a.op.Unlock()
			var req struct {
				Target string `json:"target"`
				Code   string `json:"code"`
			}
			if e := decode(w, r, &req); e != nil {
				fail(w, e)
				return
			}
			if a.busy() {
				fail(w, errors.New("请等待当前任务完成"))
				return
			}
			target, e := validTarget(req.Target)
			if e != nil {
				fail(w, e)
				return
			}
			if len(req.Code) != 6 || strings.Trim(req.Code, "0123456789") != "" {
				fail(w, errors.New("配对码必须为6位数字"))
				return
			}
			out, e := a.Engine.Runner.Run("", "pair", target, req.Code)
			if e != nil || !strings.Contains(out, "Successfully paired") {
				fail(w, errors.New("配对未成功；请使用配对端口，不是连接端口，且保持配对页面打开"))
				return
			}
			reply(w, map[string]bool{"paired": true})
		case "/api/install":
			var req struct {
				Devices []InstallRequest `json:"devices"`
			}
			if e := decode(w, r, &req); e != nil {
				fail(w, e)
				return
			}
			if len(req.Devices) == 0 || len(req.Devices) > 200 {
				fail(w, errors.New("每次接受1至200台电视"))
				return
			}
			seen := map[string]bool{}
			tokens := map[string]bool{}
			for i := range req.Devices {
				d := &req.Devices[i]
				if d.Managed && (len(req.Devices) != 1 || !d.Register || d.CloseADB || d.SkipRebootCheck || strings.TrimSpace(d.Room) == "") {
					fail(w, errors.New("后台纳管必须逐台首装、填写房号、完成重启验证并保留维护调试，请勿使用批量首装或手填凭证模式"))
					return
				}
				t, e := validTarget(d.Target)
				if e != nil {
					fail(w, e)
					return
				}
				if seen[t] {
					fail(w, errors.New("重复电视 IP"))
					return
				}
				seen[t] = true
				d.Target = t
				d.Room = strings.TrimSpace(d.Room)
				if d.Register {
					where := ""
					if len(req.Devices) > 1 {
						where = fmt.Sprintf("第 %d 台（%s）：", i+1, t)
					}
					if strings.TrimSpace(d.Server) != "" || strings.TrimSpace(d.Token) != "" {
						fail(w, errors.New(where+"登录后台自动登记时不用填写服务器地址和设备凭证"))
						return
					}
					if e := roomProblem(d.Room); e != nil {
						fail(w, errors.New(where+e.Error()))
						return
					}
					if !a.backendStatus().LoggedIn {
						fail(w, errors.New("还没有登录后台：请在“本店配置”填写服务器 IP 和管理员账号后点“登录后台”"))
						return
					}
					continue
				}
				d.Room = ""
				// Checked before the job starts, so the window keeps what was typed and says which
				// field is wrong (1.3.2 only failed inside the job, with one combined message).
				d.Server, d.Token = normalizeServer(d.Server), strings.TrimSpace(d.Token)
				if e := connectionProblem(d.Server, d.Token); e != nil {
					if len(req.Devices) > 1 {
						e = fmt.Errorf("第 %d 台（%s）：%v", i+1, t, e)
					}
					fail(w, e)
					return
				}
				if d.Token != "" {
					if tokens[d.Token] {
						fail(w, errors.New("批量安装每台电视必须使用独立设备凭证，不能重复"))
						return
					}
					tokens[d.Token] = true
				}
			}
			id, e := randomID()
			if e != nil {
				fail(w, e)
				return
			}
			a.mu.Lock()
			if a.job != nil && a.job.State == "running" {
				a.mu.Unlock()
				fail(w, errors.New("已有任务运行"))
				return
			}
			if !a.op.TryLock() {
				a.mu.Unlock()
				fail(w, errors.New("已有设备操作运行"))
				return
			}
			a.job = &Job{ID: id, State: "running", Lines: []string{}, Results: []Result{}, At: time.Now().Format(time.RFC3339)}
			hash := a.hotel.PinHash
			a.mu.Unlock()
			go a.runInstall(req.Devices, hash)
			reply(w, map[string]string{"id": id})
		case "/api/restore":
			var req struct {
				ID          string `json:"id"`
				RebootCheck bool   `json:"reboot_check"`
				Target      string `json:"target"`
			}
			if e := decode(w, r, &req); e != nil {
				fail(w, e)
				return
			}
			s, e := a.Engine.loadSnapshot(req.ID)
			if e != nil {
				fail(w, e)
				return
			}
			if req.Target != "" {
				if _, e = validTarget(req.Target); e != nil {
					fail(w, e)
					return
				}
			}
			id, e := randomID()
			if e != nil {
				fail(w, e)
				return
			}
			a.mu.Lock()
			if a.job != nil && a.job.State == "running" {
				a.mu.Unlock()
				fail(w, errors.New("已有任务运行"))
				return
			}
			if !a.op.TryLock() {
				a.mu.Unlock()
				fail(w, errors.New("已有设备操作运行"))
				return
			}
			a.job = &Job{ID: id, State: "running", Lines: []string{}, Results: []Result{}, At: time.Now().Format(time.RFC3339)}
			a.mu.Unlock()
			go func() {
				defer a.op.Unlock()
				log := func(line string) {
					a.mu.Lock()
					defer a.mu.Unlock()
					a.job.Lines = append(a.job.Lines, time.Now().Format("15:04:05")+" "+s.Target+" · "+line)
					a.saveJobLocked()
				}
				log("开始还原备份 " + s.ID)
				finalTarget := s.Target
				e := a.Engine.restore(s, RestoreOptions{RebootCheck: req.RebootCheck, Log: log, Target: req.Target, Moved: func(t string) {
					finalTarget = t
					if cur, le := a.Engine.loadSnapshot(s.ID); le == nil {
						cur.Target = t
						a.Engine.saveSnapshot(cur)
					}
				}})
				s.Target = finalTarget
				message := "已还原到这份备份记录的状态（原厂桌面、绑定；首次安装的电视已卸载影院），请在电视核对"
				if req.RebootCheck {
					message += "；重启复查已通过"
				}
				result := Result{Target: s.Target, State: "restored", Backup: s.ID, Message: message}
				if errors.Is(e, errRestoreNotRechecked) {
					result.Message, e = e.Error(), nil
				}
				if e != nil {
					result.State = "failed"
					result.Message = e.Error()
				}
				a.mu.Lock()
				a.job.Results = append(a.job.Results, result)
				a.job.State = "finished"
				a.saveJobLocked()
				a.mu.Unlock()
			}()
			reply(w, map[string]string{"id": id})
		case "/api/backups":
			entries, _ := os.ReadDir(filepath.Join(a.Engine.State, "backups"))
			all := []Snapshot{}
			for _, e := range entries {
				if e.IsDir() {
					if s, err := a.Engine.loadSnapshot(e.Name()); err == nil {
						all = append(all, s)
					}
				}
			}
			reply(w, all)
		case "/api/report":
			a.mu.Lock()
			defer a.mu.Unlock()
			w.Header().Set("Content-Disposition", `attachment; filename="installation-report.json"`)
			reply(w, a.job)
		default:
			http.NotFound(w, r)
		}
	})
}
func (a *App) backendStatus() BackendStatus {
	if a.backend == nil {
		return BackendStatus{}
	}
	return a.backend.Status()
}

func (a *App) busy() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.job != nil && a.job.State == "running"
}
func (a *App) saveJobLocked() {
	if a.job != nil {
		atomicJSON(filepath.Join(a.Engine.State, "reports", a.job.ID+".json"), a.job)
	}
}
func latestJob(state string) *Job {
	entries, _ := os.ReadDir(filepath.Join(state, "reports"))
	type candidate struct {
		name string
		at   time.Time
	}
	var files []candidate
	for _, v := range entries {
		if v.IsDir() || !regexp.MustCompile(`^[a-f0-9]{32}\.json$`).MatchString(v.Name()) {
			continue
		}
		info, e := v.Info()
		if e == nil && info.Size() < 4<<20 {
			files = append(files, candidate{v.Name(), info.ModTime()})
		}
	}
	sort.Slice(files, func(i, j int) bool { return files[i].at.After(files[j].at) })
	for _, v := range files {
		b, e := os.ReadFile(filepath.Join(state, "reports", v.name))
		if e != nil {
			continue
		}
		var j Job
		if json.Unmarshal(b, &j) != nil || j.ID+".json" != v.name {
			continue
		}
		if j.State == "running" {
			j.State = "interrupted"
			j.Lines = append(j.Lines, "上次程序已退出，任务未完成；请核对电视状态，必要时从备份恢复。")
			atomicJSON(filepath.Join(state, "reports", v.name), j)
		}
		if j.At == "" {
			j.At = v.at.Format(time.RFC3339)
		}
		j.Restored = true
		return &j
	}
	return nil
}
func (a *App) runInstall(devices []InstallRequest, hash string) {
	defer a.op.Unlock()
	for _, req := range devices {
		log := func(line string) {
			a.mu.Lock()
			defer a.mu.Unlock()
			a.job.Lines = append(a.job.Lines, time.Now().Format("15:04:05")+" "+req.Target+" · "+line)
			a.saveJobLocked()
		}
		a.mu.Lock()
		before := len(a.job.Lines)
		a.mu.Unlock()
		s, e := a.Engine.install(req, hash, log)
		a.mu.Lock()
		untouched := len(a.job.Lines) == before
		a.mu.Unlock()
		res := resultFor(req.Target, s, e, untouched)
		if e == nil && s.ADBCloseRequested {
			host, port, _ := net.SplitHostPort(s.Target)
			time.Sleep(time.Second)
			res.ADBClosed = true
			ports := []string{port}
			if port != "5555" {
				ports = append(ports, "5555")
			}
			for _, p := range ports {
				conn, ce := net.DialTimeout("tcp", net.JoinHostPort(host, p), time.Second)
				if conn != nil {
					conn.Close()
				}
				if ce == nil {
					res.ADBClosed = false
				}
			}
			if !res.ADBClosed {
				res.Message += "；调试端口仍可连接，请在电视关闭调试开关"
			} else {
				res.Message += "；调试端口当前不可连接，系统开关请在电视确认"
			}
		}
		a.mu.Lock()
		a.job.Results = append(a.job.Results, res)
		a.saveJobLocked()
		a.mu.Unlock()
	}
	a.mu.Lock()
	a.job.State = "finished"
	a.saveJobLocked()
	a.mu.Unlock()
}

// resultFor turns one TV's outcome into what the window shows (the debugging-port probe after a
// requested close is added by the caller).
func resultFor(target string, s Snapshot, e error, untouched bool) Result {
	res := Result{Target: target, State: "pending_hardware", Backup: s.ID, Tier: s.Tier, RebootVerified: s.RebootVerified, Message: "自动检查通过，待实物遥控、播放和待机验收", Room: s.BackendRoom, ServerOnline: s.ServerOnline}
	if e == nil {
		res.ConnectionState = "saved_unverified"
		switch {
		case !s.ServerConfigured:
			res.State = "pending_server"
			res.ConnectionState = "pending"
			res.Message = "电视端安装与PIN自动检查通过，服务器可稍后接入；遥控、待机待实物验收，接入后再验播放"
		case s.ServerOnline:
			res.ConnectionState = "online"
			res.Message = "已接入后台并确认电视上线（客房设备：" + s.BackendRoom + "，可在后台改房间名）；自动检查通过，待实物遥控、播放和待机验收"
		case s.BackendKept:
			res.Message = "电视原有的后台连接保留（客房设备：" + s.BackendRoom + "），但这次没等到电视再次上线，请在后台查看；自动检查通过，待实物遥控、播放和待机验收"
		case s.BackendDevice != "":
			res.Message = "已在后台登记（客房设备：" + s.BackendRoom + "）并写入电视，但后台暂未看到电视上线，请检查电视到服务器的网络；自动检查通过，待实物遥控、播放和待机验收"
		}
		// Whatever else happened, say that the registration asked for did not take place.
		if s.BackendNote != "" {
			res.Message = s.BackendNote + "；" + res.Message
		}
		if s.ManagedRequested {
			res.ManagedReady = s.ManagedReady
			if s.ManagedReady {
				res.Message += "；已核验纳入后台全程维护"
			} else {
				res.State = "pending_management"
				res.Message = "电视已安装，但后台纳管尚未完成：" + s.BackendNote + "；请在离开客房前完成服务器维护授权核验"
			}
		}
		if s.RebootVerified {
			res.Message += "；重启验证（软件重启）已通过；拔电开机、遥控待机唤醒仍待现场验收"
		} else if !s.RebootUnreachable {
			res.Message += "；未做重启验证，开机接管（含拔电开机）需到电视前确认"
		}
		if s.RebootUnreachable {
			res.State = "reboot_unverified"
			res.Message = "安装与本地检查已通过，但重启后无法重新连接电视调试：请到电视前确认开机是否进入影院；没有关闭调试。不正常时在电视重新开启调试后用备份 " + s.ID + " 还原"
		}
	}
	if e != nil {
		res.State = "failed"
		res.Message = e.Error()
		if untouched {
			// Stopped by the checks that run before the TV is contacted.
			res.Message += "（未连接电视，没有改动）"
		}
	}
	if s.ACPowerRequested {
		res.ACPower = "unsupported"
		if s.NativeACPower == nil && e != nil && verifiedACPower(s.Facts) {
			res.ACPower = "not_confirmed"
		} else if s.NativeACPower != nil {
			res.ACPower = "not_confirmed"
			if e == nil && s.NativeACPower.Readback == "direct" {
				res.ACPower = "configured_pending_physical_test"
				res.Message += "；通电模式已读回direct，实际拔电亮屏待现场验收"
			}
		} else {
			res.Message += "；本型号/固件暂不支持自动配置通电开机"
		}
	}
	return res
}

func verifyBundle(root string) error {
	b, e := os.ReadFile(filepath.Join(root, "manifest.json"))
	if e != nil {
		return errors.New("缺少完整便携包 manifest.json，请从完整压缩包启动")
	}
	var m struct {
		Files map[string]string `json:"files"`
	}
	if e = json.Unmarshal(b, &m); e != nil {
		return e
	}
	for _, critical := range []string{"payload/hotel-standard.apk", "payload/hotel-guard.apk", "payload/upgrade-policy.json", "catalog.json", "helpers/power-mode.jar"} {
		if _, ok := m.Files[critical]; !ok {
			return errors.New("包清单缺少必要文件")
		}
	}
	for name, want := range m.Files {
		p := filepath.Clean(filepath.FromSlash(name))
		if filepath.IsAbs(p) || p == ".." || strings.HasPrefix(p, ".."+string(os.PathSeparator)) {
			return errors.New("包清单路径越界")
		}
		b, er := os.ReadFile(filepath.Join(root, p))
		if er != nil {
			return er
		}
		sum := sha256.Sum256(b)
		if hex.EncodeToString(sum[:]) != want {
			return fmt.Errorf("文件校验失败：%s", name)
		}
	}
	return nil
}
func main() {
	if len(os.Args) == 3 && os.Args[1] == adbWorkerMode {
		if err := runADBWorker(os.Args[2]); err != nil {
			fatal(err)
		}
		return
	}
	exe, _ := os.Executable()
	rootFlag := flag.String("bundle", filepath.Dir(exe), "完整便携包目录")
	stateFlag := flag.String("state", "", "本地私有数据目录，默认沿用原资料或系统固定目录")
	port := flag.Int("port", 0, "操作窗口端口，0为自动选择")
	noBrowser := flag.Bool("no-browser", false, "不自动打开浏览器")
	lan := flag.Bool("lan", false, "手机通过局域网网页操作，当前电脑/Ubuntu执行安装")
	lanIP := flag.String("lan-ip", "", "手机入口绑定的本机局域网IPv4，默认自动选择唯一地址")
	flag.Parse()
	root, _ := filepath.Abs(*rootFlag)
	if e := verifyBundle(root); e != nil {
		fatal(e)
	}
	catalog, e := loadCatalog(filepath.Join(root, "catalog.json"))
	if e != nil {
		fatal(e)
	}
	configRoot, _ := os.UserConfigDir()
	state, e := stateDirectory(root, *stateFlag, os.Getenv("HOTEL_INSTALLER_STATE"), configRoot)
	if e != nil {
		fatal(e)
	}
	if e = os.MkdirAll(state, 0700); e != nil {
		fatal(e)
	}
	lock, e := lockState(filepath.Join(state, "instance.lock"))
	if e != nil {
		fatal(e)
	}
	defer lock.Close()
	var hotel Hotel
	if b, er := os.ReadFile(filepath.Join(state, "hotel.json")); er == nil {
		if er = json.Unmarshal(b, &hotel); er != nil {
			fatal(errors.New("本店 PIN 配置损坏，请保留 data 并恢复备份"))
		}
	}
	adbName := "adb"
	if runtime.GOOS == "windows" {
		adbName += ".exe"
	}
	adbPath := filepath.Join(root, "platform-tools", adbName)
	rel, _ := filepath.Rel(root, adbPath)
	var manifest struct {
		Files map[string]string `json:"files"`
	}
	b, _ := os.ReadFile(filepath.Join(root, "manifest.json"))
	json.Unmarshal(b, &manifest)
	if manifest.Files[filepath.ToSlash(rel)] == "" {
		fatal(errors.New("清单缺少对应操作系统的 ADB"))
	}
	if _, e = runtimeCheck(ADB{Path: adbPath, Port: "15047"}); e != nil {
		fatal(e)
	}
	adb, e := startADBSession(adbPath)
	if e != nil {
		fatal(e)
	}
	defer adb.Close()
	listenIP, e := installerListenIP(*lan, *lanIP)
	if e != nil {
		fatal(e)
	}
	if *lan && *port == 0 {
		*port = 18795
	}
	ln, e := net.Listen("tcp4", net.JoinHostPort(listenIP, fmt.Sprint(*port)))
	if e != nil {
		fatal(e)
	}
	token, e := randomID()
	if e != nil {
		fatal(e)
	}
	backend := &Backend{}
	a := &App{Engine: Engine{Runner: adb, Catalog: catalog, Root: root, State: state, Backend: backend}, Token: token, Host: ln.Addr().String(), hotel: hotel, job: latestJob(state), backend: backend}
	if *lan {
		a.Pairing, e = newBrowserPairing()
		if e != nil {
			fatal(e)
		}
		fmt.Printf("手机与电视/盒子、Ubuntu连接同一局域网。\n手机打开：http://%s/\n手机连接码：%s（10分钟内有效）\n", a.Host, a.Pairing.Code)
	}
	target := "http://" + a.Host + "/#" + token
	fmt.Println("酒店电视安装工具 v" + version + "\n请保持此程序运行，操作窗口：\n" + target + "\n本地备份：" + state)
	if !*noBrowser {
		switch runtime.GOOS {
		case "darwin":
			exec.Command("open", target).Start()
		case "windows":
			exec.Command("rundll32", "url.dll,FileProtocolHandler", target).Start()
		default:
			exec.Command("xdg-open", target).Start()
		}
	}
	server := &http.Server{Handler: a.handler(), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	ctx, stop := signal.NotifyContext(context.Background(), installerSignals()...)
	defer stop()
	served := make(chan error, 1)
	go func() { served <- server.Serve(ln) }()
	select {
	case <-ctx.Done():
		fmt.Println("正在退出：停止接收新操作，等待当前检测、安装或还原完成后关闭本次专用 ADB。")
		drainInstaller(server, a)
		e = <-served
	case e = <-served:
		drainInstaller(server, a)
	}
	if e != nil && !errors.Is(e, http.ErrServerClosed) {
		fmt.Fprintln(os.Stderr, e)
	}
}

func drainInstaller(server *http.Server, a *App) {
	server.Shutdown(context.Background())
	// Install/restore continue after their HTTP request has returned. Keep ADB alive until
	// their operation lock is released; a normal exit must not cut off automatic rollback.
	a.op.Lock()
	a.op.Unlock()
}
func fatal(e error) { fmt.Fprintln(os.Stderr, e); os.Exit(1) }
