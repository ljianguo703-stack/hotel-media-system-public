package main

import (
	"errors"
	"net/http"
	"os"
	"path/filepath"
)

type Attachment struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	File      string `json:"file"`
	Note      string `json:"note"`
	Available bool   `json:"available"`
	Bytes     int64  `json:"bytes"`
}

func (e Engine) attachments() []Attachment {
	rows := []Attachment{
		{ID: "ics", Title: "Android 4.0 实验版", File: "android40-experiment.zip", Note: "含guard APK、源码与说明。API15模拟器部分通过；字幕样本失败，实体型号未放行。"},
		{ID: "kitkat", Title: "Android 4.4 候选版", File: "android44-candidate.zip", Note: "含两种APK、源码与说明。API19模拟器通过；实体型号待验。"},
		{ID: "skill", Title: "历史安装接管与恢复经验", File: "tv-native-takeover-skill.zip", Note: "保留旧版验证/恢复资料；2.0盒子、手机入口与通电开机范围以随包新说明和当前方法库为准。"},
	}
	for i := range rows {
		if s, err := os.Stat(filepath.Join(e.Root, "extras", rows[i].File)); err == nil && s.Mode().IsRegular() {
			rows[i].Available = true
			rows[i].Bytes = s.Size()
		}
	}
	return rows
}
func (e Engine) attachment(w http.ResponseWriter, r *http.Request) error {
	if r.Method != "GET" {
		return errors.New("附件仅接受GET")
	}
	for _, row := range e.attachments() {
		if row.ID != r.URL.Query().Get("id") {
			continue
		}
		if !row.Available {
			return errors.New("附件缺失，请重新解压完整便携包")
		}
		w.Header().Set("Content-Type", "application/zip")
		w.Header().Set("Content-Disposition", `attachment; filename="`+row.File+`"`)
		http.ServeFile(w, r, filepath.Join(e.Root, "extras", row.File))
		return nil
	}
	return errors.New("没有此附件")
}
