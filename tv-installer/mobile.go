package main

import (
	"crypto/rand"
	"crypto/subtle"
	"errors"
	"fmt"
	"math/big"
	"net"
	"strings"
	"sync"
	"time"
)

// BrowserPairing grants the existing installer session to a phone on the local network.
// Its short code stays in the Ubuntu terminal, never in the HTML or an unauthenticated API.
type BrowserPairing struct {
	mu       sync.Mutex
	Code     string
	Expires  time.Time
	window   time.Time
	failures int
	now      func() time.Time
}

func newBrowserPairing() (*BrowserPairing, error) {
	n, err := rand.Int(rand.Reader, big.NewInt(100000000))
	if err != nil {
		return nil, err
	}
	return &BrowserPairing{Code: fmt.Sprintf("%08d", n.Int64()), Expires: time.Now().Add(10 * time.Minute), now: time.Now}, nil
}

func (p *BrowserPairing) verify(code string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.now()
	if !now.Before(p.Expires) {
		return errors.New("手机连接码已过期，请在Ubuntu结束并重新启动手机安装入口")
	}
	if p.window.IsZero() || now.Sub(p.window) >= time.Minute {
		p.window, p.failures = now, 0
	}
	if p.failures >= 5 {
		return errors.New("连接码尝试过多，请一分钟后重试")
	}
	if subtle.ConstantTimeCompare([]byte(strings.TrimSpace(code)), []byte(p.Code)) != 1 {
		p.failures++
		return errors.New("手机连接码不正确，请查看Ubuntu终端显示的8位码")
	}
	return nil
}

// Bind only an explicitly selected private interface. Never listen on all public interfaces.
func installerListenIP(lan bool, requested string) (string, error) {
	if !lan {
		if requested != "" {
			return "", errors.New("--lan-ip需要同时启用--lan")
		}
		return "127.0.0.1", nil
	}
	interfaces, err := net.Interfaces()
	if err != nil {
		return "", err
	}
	var candidates []string
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			ip, _, err := net.ParseCIDR(addr.String())
			if err != nil || ip.To4() == nil || !ip.IsPrivate() {
				continue
			}
			if requested != "" && ip.String() == requested {
				return requested, nil
			}
			if strings.HasPrefix(iface.Name, "docker") || strings.HasPrefix(iface.Name, "veth") || strings.HasPrefix(iface.Name, "br-") || strings.HasPrefix(iface.Name, "utun") || strings.HasPrefix(iface.Name, "tun") || strings.HasPrefix(iface.Name, "wg") {
				continue
			}
			candidates = append(candidates, ip.String())
		}
	}
	if requested != "" {
		return "", errors.New("--lan-ip必须是Ubuntu本机已有的局域网IPv4地址")
	}
	if len(candidates) != 1 {
		return "", errors.New("无法唯一确定局域网地址，请加 --lan-ip=Ubuntu的局域网IP；不要使用公网IP")
	}
	return candidates[0], nil
}
