package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"time"
)

const adbWorkerMode = "--internal-adb-session"

type adbReady struct {
	Port string `json:"port"`
	PID  int    `json:"pid"`
}

// Each application launch owns a fresh foreground ADB, never the shared 15047/5037 daemon.
// A small child supervisor watches the parent's stdin pipe: even an abrupt parent death
// closes that pipe, and only the supervisor's own native process is stopped and reaped.
type adbSession struct {
	ADB
	PID     int
	control io.WriteCloser
	worker  *exec.Cmd
	done    chan struct{}
	once    sync.Once
}

func startADBSession(path string) (*adbSession, error) {
	exe, err := os.Executable()
	if err != nil {
		return nil, err
	}
	return openADBSession(exec.Command(exe, adbWorkerMode, path), path)
}

func openADBSession(worker *exec.Cmd, path string) (*adbSession, error) {
	isolateADBWorker(worker)
	input, err := worker.StdinPipe()
	if err != nil {
		return nil, err
	}
	output, err := worker.StdoutPipe()
	if err != nil {
		input.Close()
		return nil, err
	}
	worker.Stderr = os.Stderr
	if err = worker.Start(); err != nil {
		input.Close()
		output.Close()
		return nil, err
	}
	s := &adbSession{ADB: ADB{Path: path}, control: input, worker: worker, done: make(chan struct{})}
	go func() { worker.Wait(); close(s.done) }()
	type result struct {
		ready adbReady
		err   error
	}
	ready := make(chan result, 1)
	go func() {
		var r result
		r.err = json.NewDecoder(io.LimitReader(output, 4096)).Decode(&r.ready)
		ready <- r
	}()
	select {
	case r := <-ready:
		port, pe := strconv.Atoi(r.ready.Port)
		if r.err == nil && pe == nil && port > 0 && port < 65536 && r.ready.PID > 0 {
			s.Port, s.PID = r.ready.Port, r.ready.PID
			return s, nil
		}
	case <-s.done:
	case <-time.After(15 * time.Second):
	}
	s.Close()
	return nil, errors.New("本次工具专用的 ADB 后台未能启动；请保留终端错误信息，退出后重开工具")
}

func (s *adbSession) Run(serial string, args ...string) (string, error) {
	select {
	case <-s.done:
		return "", errors.New("本次工具的 ADB 后台已经退出，请退出并重新打开工具；不会复用其他工具的后台")
	default:
		return s.ADB.Run(serial, args...)
	}
}

func (s *adbSession) Close() {
	s.once.Do(func() {
		s.control.Close()
		// The native child is local, foreground and killable; allow its supervisor to reap it.
		select {
		case <-s.done:
		case <-time.After(5 * time.Second):
			// Never use kill-server here: that might affect an unrelated ADB server.
			fmt.Fprintln(os.Stderr, "ADB 专用后台仍在退出，请保留本工具终端信息")
		}
	})
}

func adbWorkerEnv() []string {
	var env []string
	for _, v := range os.Environ() {
		key, _, _ := strings.Cut(v, "=")
		switch strings.ToUpper(key) {
		case "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_PORT", "ANDROID_ADB_SERVER_ADDRESS", "ADB_MDNS_AUTO_CONNECT", "ADB_TRACE":
			continue
		}
		env = append(env, v)
	}
	return append(env, "ADB_MDNS_AUTO_CONNECT=0")
}

func adbServerReady(port string) bool {
	c, err := net.DialTimeout("tcp4", "127.0.0.1:"+port, 150*time.Millisecond)
	if err != nil {
		return false
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(300 * time.Millisecond))
	if _, err = io.WriteString(c, "000chost:version"); err != nil {
		return false
	}
	var response [12]byte
	_, err = io.ReadFull(c, response[:])
	return err == nil && string(response[:8]) == "OKAY0004"
}

func runADBWorker(path string) error {
	ctx, stop := signal.NotifyContext(context.Background(), installerSignals()...)
	defer stop()
	parentGone := make(chan struct{})
	go func() { io.Copy(io.Discard, os.Stdin); close(parentGone) }()
	// Choose a fresh loopback port. A collision is an explicit start failure, never a reason
	// to adopt the server already on that port. Native process liveness is checked as well.
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return err
	}
	port := strconv.Itoa(ln.Addr().(*net.TCPAddr).Port)
	ln.Close()
	// Pinned ADB accepts a hostname for clients, but its listener only accepts tcp:PORT.
	// Without -a the native listener is loopback-only.
	cmd := exec.Command(path, "-L", "tcp:"+port, "server", "nodaemon")
	cmd.Env = adbWorkerEnv()
	cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
	if err = cmd.Start(); err != nil {
		return err
	}
	exited := make(chan struct{})
	go func() { cmd.Wait(); close(exited) }()
	defer func() { cmd.Process.Kill(); <-exited }()
	deadline := time.NewTimer(10 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(30 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-parentGone:
			return nil
		case <-ctx.Done():
			return nil
		case <-exited:
			return errors.New("随包 ADB 前台服务提前退出")
		case <-deadline.C:
			return errors.New("等待本次专用 ADB 服务超时")
		case <-tick.C:
			if !adbServerReady(port) {
				continue
			}
			select {
			case <-exited:
				return errors.New("本次 ADB 端口未由自己的进程持有")
			default:
			}
			if err = json.NewEncoder(os.Stdout).Encode(adbReady{Port: port, PID: cmd.Process.Pid}); err != nil {
				return err
			}
			select {
			case <-parentGone:
				return nil
			case <-ctx.Done():
				return nil
			case <-exited:
				return errors.New("本次工具的 ADB 服务已退出")
			}
		}
	}
}
