package main

import "testing"

func TestParseNameStatus(t *testing.T) {
	changes := parseNameStatus("M\x00changed.go\x00R100\x00old.go\x00new.go\x00D\x00removed.go\x00")
	if len(changes) != 3 {
		t.Fatalf("got %d changes, want 3", len(changes))
	}
	if changes[1] != [3]string{"R", "new.go", "old.go"} {
		t.Fatalf("rename = %#v", changes[1])
	}
}

func TestBackendFileFilters(t *testing.T) {
	if !indexable("pkg/main.go", codannaSuffixes) {
		t.Error("Go should be Codanna-indexable")
	}
	if indexable(".github/workflows/ci.yml", codannaSuffixes) {
		t.Error("YAML should not be Codanna-indexable")
	}
	if !indexable(".github/workflows/ci.yml", cocoIndexSuffixes) {
		t.Error("YAML should be CocoIndex-indexable")
	}
}
