package main

import (
	"errors"
	"net"
	"strings"
	"time"
)

// registration is what this run did in the backend for one TV.
type registration struct {
	ID       string
	Room     string
	Created  bool  // a new entry: moved to the recycle bin again if the install is rolled back
	Kept     bool  // the TV was already connected to this backend; nothing was changed there
	Baseline int64 // last_seen before the TV was given the credential
}

func targetHost(target string) string {
	if host, _, err := net.SplitHostPort(target); err == nil {
		return host
	}
	return target
}

// registerTV gets the TV a connection from the backend the operator logged into. A TV that is
// already connected to that backend keeps its connection (a re-install must not issue new
// credentials or add a second entry). It returns the server and credential to write, or empty ones
// when nothing is to be written; a backend that fails does not stop the install, the connection can
// then be set up later.
func (e Engine) registerTV(target string, snap Snapshot, f Facts, room string, log func(string)) (*registration, string, string, string) {
	id := backendDeviceID(snap.Identity)
	reg := &registration{ID: id}
	status, se := shell(e.Runner, target, "am", "broadcast", "-a", pkg+".CONFIG", "-n", pkg+"/.guard.ConfigReceiver", "--ez", "provision_status", "true")
	configured, _, serr := provisioningState(status)
	if se != nil || serr != nil {
		// Unknown whether the TV uses a credential of this backend: issuing a new one could cut off a
		// connection that works.
		note := "读不到电视现有的服务器连接状态，没有在后台登记（避免换掉电视正在用的凭证）"
		log(note)
		return nil, "", "", note
	}
	if configured {
		d, found, err := e.Backend.Lookup(id, targetHost(target))
		if err != nil {
			note := "后台查询未成功（" + err.Error() + "），电视已有的服务器连接保持不变"
			log(note)
			return nil, "", "", note
		}
		if found {
			reg.ID, reg.Room, reg.Kept, reg.Baseline = d.ID, d.Room, true, d.LastSeen
			log("这台电视已接入这台后台（客房设备：" + d.Room + "），保留原连接，不换发凭证")
			return reg, "", "", ""
		}
		log("电视原有的服务器连接不属于这台后台，改接到 " + e.Backend.TVOrigin() + "（原连接已备份，还原时会放回）")
	}
	if room == "" {
		room = defaultRoom(target)
	}
	token, entry, created, err := e.Backend.Register(id, room, strings.TrimSpace(f.Brand+" "+f.Model))
	if err != nil {
		note := "后台登记未成功：" + err.Error()
		log(note + "；电视的服务器连接没有改动，服务器可稍后接入")
		return nil, "", "", note
	}
	// The baseline comes from the backend's own record before the credential changed: only a request
	// made with the new credential can move last_seen past it.
	reg.Created, reg.Room, reg.Baseline = created, entry.Room, entry.LastSeen
	if created {
		log("已在后台“客房设备”登记这台电视：" + reg.Room + "（设备 " + id + "），取得它的独立设备凭证")
	} else {
		log("这台电视以前登记过（设备 " + id + "，" + reg.Room + "），已换发新凭证；后台里的房间名、分组保持不变")
	}
	tv := e.Backend.TVOrigin()
	if _, err := e.preflightServer(tv, token); err != nil {
		log("提示：电脑侧检查电视服务未通过（" + err.Error() + "）；仍写入电视，以电视自己上线为准")
	} else {
		log("电脑侧已用新凭证访问电视服务 " + tv + "：通过")
	}
	return reg, tv, token, ""
}

// confirmOnline waits for the backend to hear from the TV itself: the client fetches its catalog as
// soon as it starts with the new connection. The tool's own check is sent as a probe and does not
// count, so this proves the TV's network path, not the PC's.
func (e Engine) confirmOnline(reg *registration, log func(string)) bool {
	log("确认连接：等后台看到这台电视上线（最多约 45 秒）")
	for i := 0; i < 15; i++ {
		e.wait(3 * time.Second)
		d, err := e.Backend.Seen(reg.ID)
		if errors.Is(err, errBackendSession) {
			log("后台登录已失效，无法确认上线；电视的连接信息已保存")
			return false
		}
		if err == nil && d.LastSeen > reg.Baseline {
			if d.Room != "" {
				reg.Room = d.Room
			}
			log("连接已确认：后台看到这台电视上线（客房设备：" + reg.Room + "）")
			return true
		}
	}
	log("电视已保存连接信息，但约 45 秒内后台没看到它上线：请检查电视能否访问 " + e.Backend.TVOrigin() + "（同一网段、服务器防火墙放行电视服务端口）；之后可在后台“客房设备”查看是否上线")
	return false
}
