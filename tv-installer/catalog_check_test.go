package main

import "testing"

func TestShippedCatalogLoadsAndKeepsFieldVerifiedModel(t *testing.T) {
	c, err := loadCatalog("catalog.json")
	if err != nil {
		t.Fatal(err)
	}
	d := decide(c, Facts{Brand: "Xiaomi", Model: "MiTV-ASTP0", Device: "mulan", SDK: 28, Fingerprint: "Xiaomi/mulan/mulan:9/PPR1.180610.011/OS3.0.104.0.PSTAATV:user/release-keys"})
	if !d.Allowed || d.Tier != "verified" || d.Route != "standard" {
		t.Fatal("the hotel's own verified MiTV-ASTP0 firmware must stay one-click", d)
	}
	if d = decide(c, Facts{Brand: "Xiaomi", Model: "MiTV-ASTP0", Device: "mulan", SDK: 28, Fingerprint: "Xiaomi/mulan/mulan:9/other"}); d.Allowed || !d.Trial {
		t.Fatal("another firmware of the same model is a trial", d)
	}
	// Aerial ❌ (firmware clears accessibility ~every 52 s): not recommended, but may be tried.
	if d = decide(c, Facts{Brand: "TCL", Manufacturer: "FFALCON", Model: "Smart TV Pro", Device: "T966D5", SDK: 34, Fingerprint: "TCL/T966D5_4K_CN/T966D5:14/UTT2.250416.001/AU01:user/release-keys", CryptoType: "file"}); d.Tier != "avoid" || !d.Trial || d.Allowed || d.Route != "guard" {
		t.Fatal("T966D5 must be 'avoid' (trial with guard)", d)
	}
	// Aerial ✅ with exact identifiers and the same build: one-click.
	if d = decide(c, Facts{Brand: "Xiaomi", Model: "MiTV-MFFU0", Device: "finch", SDK: 34}); d.Tier != "proven" || !d.Allowed || d.Route != "standard" {
		t.Fatal("Aerial-proven MFFU0 must be one-click", d)
	}
	// Same model on FBE: we must switch to guard, so Aerial's standard-build evidence is only a trial.
	if d = decide(c, Facts{Brand: "Xiaomi", Model: "MiTV-MFFU0", Device: "finch", SDK: 34, CryptoType: "file"}); d.Allowed || !d.Trial || d.Route != "guard" {
		t.Fatal("evidence for another build must not be one-click", d)
	}
	// Chip-vendor reference identifiers are shared by other makers: never one-click.
	for _, f := range []Facts{
		{Brand: "Konka", Model: "X5", Device: "X5", SDK: 26, Fingerprint: "Mstar/X5/X5:8.0.0/OPR5.170623.014/1:user/release-keys"},
		{Brand: "Changhong", Model: "MStar Android TV", Device: "almond", SDK: 23},
		{Brand: "Haier", Model: "x", Device: "mt5879_cd", SDK: 30, Fingerprint: "MediaTek/mt5879_cd/mt5879:11/RP1A.200720.011/1:user/dev-keys"},
		{Brand: "Panasonic", Model: "y", Device: "BRAVIA_VH1", SDK: 31},
	} {
		if d = decide(c, f); d.Allowed {
			t.Fatal("cross-maker identifier gave a one-click install", f, d.Profile)
		}
	}
	if d = decide(c, Facts{Brand: "Sony", Model: "BRAVIA", Device: "BRAVIA_VH1", SDK: 31}); d.Tier != "proven" {
		t.Fatal("Sony VH1 with Sony brand", d)
	}
	if d = decide(c, Facts{Brand: "Hisense", Model: "VIDAA_TV", Device: "NOVA72671", SDK: 26, Fingerprint: "Hisense/VIDAA_TV/NOVA72671:8.0.0/OPR6.170623.013/V0000.01:user/release-keys"}); d.Tier != "proven" || d.Route != "standard" {
		t.Fatal("Aerial-proven Hisense 65E52D", d)
	}
	// Generic model strings alone never match evidence.
	if d = decide(c, Facts{Brand: "Hisense", Model: "VIDAA_TV", Device: "unknown-board", SDK: 26}); d.Tier == "proven" {
		t.Fatal("generic model matched", d)
	}
	// Android 6 Xiaomi (Aerial ✅): the guard-only legacy route is new, so a trial with the evidence shown.
	if d = decide(c, Facts{Brand: "Xiaomi", Model: "MiTV4-ANSM0", Device: "xmen", SDK: 23, Fingerprint: "Xiaomi/xmen/xmen:6.0.1/MHC19J/1:user/release-keys"}); d.Allowed || !d.Trial || d.Route != "guard" || len(d.Warnings) == 0 {
		t.Fatal("Android 6 proven must be a guard trial", d)
	}
	if d = decide(c, Facts{Brand: "Xiaomi", Model: "MiTV2-49", Device: "entrapment", SDK: 18}); d.Tier != "blocked" {
		t.Fatal("Android 4.x must stay out of the main installer", d)
	}
	if d = decide(c, Facts{Brand: "Xiaomi", Model: "MiTV-MFTR0", Device: "freeguy", SDK: 30, CryptoType: "file"}); !d.Trial || d.Route != "guard" {
		t.Fatal("unverified FBE Android 10+ must be a guard trial", d)
	}
}
