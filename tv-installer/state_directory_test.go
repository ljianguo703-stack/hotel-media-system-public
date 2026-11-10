package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestNewVersionReusesPersistentPINAndBootHistory(t *testing.T) {
	root := t.TempDir()
	config := filepath.Join(root, "config")
	a, err := stateDirectory(filepath.Join(root, "version-a"), "", "", config)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.MkdirAll(a, 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"hotel.json", "adb-off-at-boot.json"} {
		if err = os.WriteFile(filepath.Join(a, name), []byte("preserved-private-record"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	b, err := stateDirectory(filepath.Join(root, "version-b"), "", "", config)
	if err != nil || b != a {
		t.Fatal(a, b, err)
	}
	for _, name := range []string{"hotel.json", "adb-off-at-boot.json"} {
		data, err := os.ReadFile(filepath.Join(b, name))
		if err != nil || string(data) != "preserved-private-record" {
			t.Fatal(name, err)
		}
	}
}

func TestExistingDistinctStoresAreNeverMerged(t *testing.T) {
	base := t.TempDir()
	bundle := filepath.Join(base, "01_最新版交付", "03_工具", "Mac")
	local := filepath.Join(bundle, "data")
	canonical := filepath.Join(base, "电视安装本店资料", "data")
	for path, value := range map[string]string{local: "local-pin", canonical: "canonical-pin"} {
		if err := os.MkdirAll(path, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(path, "hotel.json"), []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
	}
	got, err := stateDirectory(bundle, "", "", filepath.Join(base, "config"))
	if err != nil || got != local {
		t.Fatal(got, err)
	}
	for path, want := range map[string]string{local: "local-pin", canonical: "canonical-pin"} {
		data, _ := os.ReadFile(filepath.Join(path, "hotel.json"))
		if string(data) != want {
			t.Fatal("Store was changed", path)
		}
	}
	// The generic bundle has no local data; an explicitly organized delivery uses its private store.
	newBundle := filepath.Join(base, "01_最新版交付", "03_新工具", "Mac")
	got, err = stateDirectory(newBundle, "", "", filepath.Join(base, "config"))
	if err != nil || got != canonical {
		t.Fatal(got, err)
	}
}

func TestExplicitStateAndEnvironmentKeepTheirPriority(t *testing.T) {
	root := t.TempDir()
	for _, tc := range []struct{ explicit, env, want string }{
		{filepath.Join(root, "explicit"), filepath.Join(root, "env"), filepath.Join(root, "explicit")},
		{"", filepath.Join(root, "env"), filepath.Join(root, "env")},
	} {
		got, err := stateDirectory(root, tc.explicit, tc.env, "")
		if err != nil || got != tc.want {
			t.Fatal(got, err)
		}
		if _, err = os.Stat(got); !os.IsNotExist(err) {
			t.Fatal("Resolution must not create a new PIN store")
		}
	}
}

func TestInvalidLegacyStoreDoesNotSilentlyCreateAnother(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "data"), []byte("preserve"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := stateDirectory(root, "", "", filepath.Join(root, "config")); err == nil {
		t.Fatal("Must stop on unreadable/invalid old store")
	}
}
