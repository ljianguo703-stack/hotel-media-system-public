package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestMobilePairingRateAndExpiry(t *testing.T) {
	now := time.Now()
	p := &BrowserPairing{Code: "12345678", Expires: now.Add(10 * time.Minute), now: func() time.Time { return now }}
	for i := 0; i < 5; i++ {
		if p.verify("00000000") == nil {
			t.Fatal("wrong code accepted")
		}
	}
	if p.verify("12345678") == nil {
		t.Fatal("rate limit bypassed")
	}
	now = now.Add(time.Minute)
	if err := p.verify("12345678"); err != nil {
		t.Fatal(err)
	}
	now = now.Add(10 * time.Minute)
	if p.verify("12345678") == nil {
		t.Fatal("expired code accepted")
	}
}

func TestMobileSessionNeedsCodeAndPreservesOriginAndHost(t *testing.T) {
	now := time.Now()
	a := &App{Host: "192.168.1.10:18795", Token: "test-session-secret", Pairing: &BrowserPairing{Code: "12345678", Expires: now.Add(time.Minute), now: func() time.Time { return now }}}
	h := a.handler()
	run := func(path, body, host, origin, token string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "http://"+a.Host+path, bytes.NewBufferString(body))
		r.Host = host
		r.RemoteAddr = "192.168.1.20:40000"
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("Origin", origin)
		r.Header.Set("X-App-Token", token)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	if w := run("/api/state", "{}", a.Host, "http://"+a.Host, ""); w.Code != http.StatusUnauthorized {
		t.Fatal(w.Code, w.Body.String())
	}
	for _, v := range []struct{ host, origin string }{{"attacker.example", "http://" + a.Host}, {a.Host, "http://attacker.example"}} {
		if w := run("/api/pair-browser", `{"code":"12345678"}`, v.host, v.origin, ""); w.Code != http.StatusForbidden {
			t.Fatal("origin/host bypass", w.Code)
		}
	}
	bad := run("/api/pair-browser", `{"code":"00000000"}`, a.Host, "http://"+a.Host, "")
	if strings.Contains(bad.Body.String(), a.Token) {
		t.Fatal("token leaked for wrong code")
	}
	w := run("/api/pair-browser", `{"code":"12345678"}`, a.Host, "http://"+a.Host, "")
	var got map[string]string
	if json.Unmarshal(w.Body.Bytes(), &got) != nil || got["token"] != a.Token {
		t.Fatal(w.Body.String())
	}
	r := httptest.NewRequest("GET", "http://"+a.Host+"/", nil)
	r.Host = a.Host
	r.RemoteAddr = "192.168.1.20:40000"
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if !strings.Contains(w.Body.String(), `name="installer-lan" content="true"`) {
		t.Fatal("LAN UI unavailable")
	}
	if strings.Contains(w.Body.String(), a.Token) || strings.Contains(w.Body.String(), a.Pairing.Code) {
		t.Fatal("secret embedded in unauthenticated HTML")
	}
	r = httptest.NewRequest("GET", "http://"+a.Host+"/", nil)
	r.Host = a.Host
	r.RemoteAddr = "203.0.113.10:40000"
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusForbidden {
		t.Fatal("public-network peer was accepted")
	}
}

func TestDesktopModeStillLoopback(t *testing.T) {
	ip, err := installerListenIP(false, "")
	if err != nil || ip != "127.0.0.1" {
		t.Fatal(ip, err)
	}
	if _, err = installerListenIP(false, "192.168.1.10"); err == nil {
		t.Fatal("LAN option silently enabled")
	}
	if _, err = installerListenIP(true, "0.0.0.0"); err == nil {
		t.Fatal("all interfaces accepted")
	}
	if _, err = installerListenIP(true, "8.8.8.8"); err == nil {
		t.Fatal("public interface accepted")
	}
}
