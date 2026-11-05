package main

import (
	"encoding/json"
	"net/url"
	"testing"
)

func TestBoxProfilesAreControlledTrialsAndRestrictionsRemain(t *testing.T) {
	c, err := loadCatalog("catalog.json")
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range []Facts{
		{Brand: "Xiaomi", Model: "MIBOX4", Device: "oneday", SDK: 28, Fingerprint: "Xiaomi/oneday/oneday:9/PI/3933:user/release-keys"},
		{Brand: "NVIDIA", Model: "P3430", Device: "sif", SDK: 30},
		{Brand: "onn", Model: "onn. 4K Plus Streaming", Device: "coffey", SDK: 34, CryptoType: "file"},
	} {
		d := decide(c, f)
		if d.Allowed || !d.Trial || d.Route != "guard" || d.Profile == nil || d.Profile.DeviceKind != "box" {
			t.Fatal(f, d)
		}
	}
	for _, f := range []Facts{
		{Brand: "google", Device: "boreal", SDK: 34},
		{Brand: "google", Device: "kirkwood", SDK: 34},
		{Brand: "skyworthbox", Model: "Q5043", Device: "cupid-p2", SDK: 29, Fingerprint: "skyworthbox/cupid_p2/cupid-p2:10/QP1A.191105.004/test"},
	} {
		d := decide(c, f)
		if d.Allowed || d.Trial || d.Tier != "blocked" {
			t.Fatal("known restricted box was offered install", f, d)
		}
	}
}

func TestEmbeddedConnectionGuidesHavePinnedSourcesAndUniqueIDs(t *testing.T) {
	b, err := assets.ReadFile("connection-guides.json")
	if err != nil {
		t.Fatal(err)
	}
	var data struct {
		Guides []struct {
			ID, Kind              string
			Steps, Notes, Sources []string
		}
	}
	if json.Unmarshal(b, &data) != nil || len(data.Guides) < 20 {
		t.Fatal("missing guide data")
	}
	seen := map[string]bool{}
	boxes := 0
	for _, g := range data.Guides {
		if g.ID == "" || seen[g.ID] || len(g.Steps) == 0 || len(g.Sources) == 0 {
			t.Fatal(g.ID)
		}
		seen[g.ID] = true
		if g.Kind == "box" {
			boxes++
		}
		for _, source := range g.Sources {
			u, err := url.Parse(source)
			if err != nil || u.Scheme != "https" || u.Hostname() == "" {
				t.Fatal(source)
			}
		}
	}
	if boxes < 8 {
		t.Fatal("missing box connection families")
	}
	b, err = assets.ReadFile("source/community-index.json")
	if err != nil {
		t.Fatal(err)
	}
	var index struct {
		Commit   string
		Examples []struct{ ID, URL string }
	}
	if json.Unmarshal(b, &index) != nil || index.Commit == "" || len(index.Examples) < 400 {
		t.Fatal("missing pinned community index")
	}
	for _, row := range index.Examples {
		u, err := url.Parse(row.URL)
		if err != nil || u.Hostname() != "github.com" {
			t.Fatal(row.URL)
		}
	}
}
