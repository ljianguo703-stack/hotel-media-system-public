package main

import (
	"errors"
	"fmt"
	"net"
	"strconv"
	"time"
)

// Optional so old/offline backends and restore fixtures retain their established contract.
type fleetRegistrar interface {
	EnrollManaged(id, target, identity string, log func(string)) error
}

func (b *Backend) EnrollManaged(id, target, identity string, log func(string)) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	host, portText, err := net.SplitHostPort(target)
	if err != nil {
		return errors.New("无法读取这台电视的维护地址")
	}
	port, err := strconv.Atoi(portText)
	if err != nil {
		return errors.New("维护端口无效")
	}
	var st struct {
		Revision int64 `json:"revision"`
	}
	if _, err = b.do("GET", "/api/admin/software", nil, &st, 4<<20); err != nil {
		return err
	}
	var reply struct {
		Job struct {
			ID string `json:"id"`
		} `json:"job"`
	}
	body := map[string]any{"id": id, "address": host, "port": port, "identity_sha256": identity, "confirm": true, "online_confirmed": true, "reboot_verified": true, "revision": st.Revision}
	if _, err = b.do("POST", "/api/admin/fleet/enroll", body, &reply, 1<<20); err != nil {
		return err
	}
	if reply.Job.ID == "" {
		return errors.New("后台未返回纳管核验任务")
	}
	log("请核对电视上的本店Ubuntu服务器调试密钥并授权；这一步仅在首次逐台纳管时完成。")
	last := ""
	for n := 0; n < 90; n++ {
		var state struct {
			Jobs []struct {
				ID      string `json:"id"`
				Status  string `json:"status"`
				Message string `json:"message"`
			} `json:"jobs"`
		}
		if _, err = b.do("GET", "/api/admin/software", nil, &state, 4<<20); err != nil {
			return err
		}
		for _, j := range state.Jobs {
			if j.ID != reply.Job.ID {
				continue
			}
			if j.Message != "" && j.Message != last {
				log("服务器纳管核验：" + j.Message)
				last = j.Message
			}
			if j.Status == "completed" {
				return nil
			}
			if j.Status == "failed" || j.Status == "waiting_authorization" || j.Status == "interrupted" || j.Status == "cancelled" {
				return fmt.Errorf("服务器纳管未完成：%s；在离开客房前处理授权并重新核验", j.Message)
			}
		}
		time.Sleep(2 * time.Second)
	}
	return errors.New("服务器纳管核验未在约3分钟内完成；此电视暂不能作为已纳管目标，请在后台核对")
}
