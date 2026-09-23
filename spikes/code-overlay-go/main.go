package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

var (
	cocoIndexSuffixes = map[string]bool{
		".c": true, ".cc": true, ".cpp": true, ".cs": true, ".go": true,
		".h": true, ".hpp": true, ".java": true, ".js": true, ".jsx": true,
		".json": true, ".kt": true, ".md": true, ".mod": true, ".php": true,
		".proto": true, ".py": true, ".rego": true, ".rs": true, ".sh": true,
		".sql": true, ".sum": true, ".swift": true, ".toml": true, ".ts": true,
		".tsx": true, ".tpl": true, ".yaml": true, ".yml": true,
	}
	codannaSuffixes = map[string]bool{
		".c": true, ".cc": true, ".cpp": true, ".cs": true, ".go": true,
		".h": true, ".hpp": true, ".java": true, ".js": true, ".jsx": true,
		".kt": true, ".php": true, ".py": true, ".rb": true, ".rs": true,
		".swift": true, ".ts": true, ".tsx": true,
	}
)

type FileChange struct {
	Path    string `json:"path"`
	Status  string `json:"status"`
	SHA256  string `json:"sha256,omitempty"`
	OldPath string `json:"old_path,omitempty"`
}

type Plan struct {
	Repository          string       `json:"repository"`
	Worktree            string       `json:"worktree"`
	Branch              string       `json:"branch"`
	HeadCommit          string       `json:"head_commit"`
	BaseRef             string       `json:"base_ref"`
	BaseCommit          string       `json:"base_commit"`
	IncludesUncommitted bool         `json:"includes_uncommitted"`
	WorkingTreeDigest   string       `json:"working_tree_digest"`
	OverlayID           string       `json:"overlay_id"`
	Files               []FileChange `json:"files"`
	PlannerElapsedMS    float64      `json:"planner_elapsed_ms"`
}

type summary struct {
	Plan             Plan    `json:"plan"`
	IndexedFiles     int     `json:"indexed_files"`
	CodannaFiles     int     `json:"codanna_files"`
	CocoIndexFiles   int     `json:"cocoindex_files"`
	Tombstones       int     `json:"tombstones"`
	StagedFiles      int     `json:"staged_files,omitempty"`
	StagingElapsedMS float64 `json:"staging_elapsed_ms,omitempty"`
}

func git(root string, args ...string) (string, error) {
	commandArgs := append([]string{"-C", root}, args...)
	command := exec.Command("git", commandArgs...)
	output, err := command.Output()
	if err != nil {
		var exitError *exec.ExitError
		if errors.As(err, &exitError) {
			return "", fmt.Errorf("git %s: %s", strings.Join(args, " "), strings.TrimSpace(string(exitError.Stderr)))
		}
		return "", err
	}
	return string(output), nil
}

func fileHash(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func indexable(path string, suffixes map[string]bool) bool {
	base := filepath.Base(path)
	if base == "Dockerfile" || base == "Makefile" || base == "Containerfile" {
		return true
	}
	return suffixes[strings.ToLower(filepath.Ext(path))]
}

func parseNameStatus(output string) [][3]string {
	fields := strings.Split(output, "\x00")
	changes := make([][3]string, 0)
	for index := 0; index < len(fields) && fields[index] != ""; {
		status := fields[index]
		index++
		if index >= len(fields) {
			break
		}
		path := fields[index]
		index++
		if strings.HasPrefix(status, "R") || strings.HasPrefix(status, "C") {
			if index >= len(fields) {
				break
			}
			changes = append(changes, [3]string{status[:1], fields[index], path})
			index++
		} else {
			changes = append(changes, [3]string{status[:1], path, ""})
		}
	}
	return changes
}

func safeRelativePath(path string) error {
	clean := filepath.Clean(filepath.FromSlash(path))
	if filepath.IsAbs(clean) || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return fmt.Errorf("unsafe Git path %q", path)
	}
	return nil
}

func buildPlan(worktree, baseRef, repository string) (Plan, error) {
	started := time.Now()
	absoluteRoot, err := filepath.Abs(worktree)
	if err != nil {
		return Plan{}, err
	}
	gitRoot, err := git(absoluteRoot, "rev-parse", "--show-toplevel")
	if err != nil {
		return Plan{}, err
	}
	gitRoot, err = filepath.Abs(strings.TrimSpace(gitRoot))
	if err != nil || filepath.Clean(gitRoot) != filepath.Clean(absoluteRoot) {
		return Plan{}, fmt.Errorf("worktree must be the Git root: %s", absoluteRoot)
	}
	head, err := git(absoluteRoot, "rev-parse", "HEAD")
	if err != nil {
		return Plan{}, err
	}
	head = strings.TrimSpace(head)
	branch, err := git(absoluteRoot, "symbolic-ref", "--quiet", "--short", "HEAD")
	if err != nil || strings.TrimSpace(branch) == "" {
		branch = "detached:" + head[:12]
	} else {
		branch = strings.TrimSpace(branch)
	}
	base, err := git(absoluteRoot, "merge-base", baseRef, "HEAD")
	if err != nil {
		return Plan{}, err
	}
	base = strings.TrimSpace(base)
	changes := map[string]FileChange{}
	uncommitted := false
	record := func(status, path, oldPath string) error {
		if err := safeRelativePath(path); err != nil {
			return err
		}
		absolutePath := filepath.Join(absoluteRoot, filepath.FromSlash(path))
		finalStatus := status
		switch status {
		case "A":
			finalStatus = "added"
		case "D":
			finalStatus = "deleted"
		case "?":
			finalStatus = "untracked"
		default:
			if _, statErr := os.Stat(absolutePath); statErr != nil {
				finalStatus = "deleted"
			} else {
				finalStatus = "modified"
			}
		}
		if oldPath != "" {
			finalStatus = "renamed"
		}
		change := FileChange{Path: filepath.ToSlash(path), Status: finalStatus, OldPath: oldPath}
		if finalStatus != "deleted" {
			if hash, hashErr := fileHash(absolutePath); hashErr == nil {
				change.SHA256 = hash
			}
		}
		changes[change.Path] = change
		if oldPath != "" {
			changes[filepath.ToSlash(oldPath)] = FileChange{Path: filepath.ToSlash(oldPath), Status: "deleted", OldPath: oldPath}
		}
		return nil
	}
	commands := [][]string{
		{"diff", "--name-status", "--find-renames", "-z", base + "...HEAD"},
		{"diff", "--name-status", "--find-renames", "-z"},
		{"diff", "--cached", "--name-status", "--find-renames", "-z"},
	}
	for index, command := range commands {
		output, commandErr := git(absoluteRoot, command...)
		if commandErr != nil {
			return Plan{}, commandErr
		}
		if index > 0 && output != "" {
			uncommitted = true
		}
		for _, change := range parseNameStatus(output) {
			if err := record(change[0], change[1], change[2]); err != nil {
				return Plan{}, err
			}
		}
	}
	untracked, err := git(absoluteRoot, "ls-files", "--others", "--exclude-standard", "-z")
	if err != nil {
		return Plan{}, err
	}
	if untracked != "" {
		uncommitted = true
	}
	for _, path := range strings.Split(untracked, "\x00") {
		if path != "" {
			if err := record("?", path, ""); err != nil {
				return Plan{}, err
			}
		}
	}
	files := make([]FileChange, 0, len(changes))
	for _, change := range changes {
		files = append(files, change)
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	digestInput, err := json.Marshal(files)
	if err != nil {
		return Plan{}, err
	}
	digestBytes := sha256.Sum256(digestInput)
	digest := hex.EncodeToString(digestBytes[:])
	if repository == "" {
		repository = filepath.Base(absoluteRoot)
	}
	return Plan{
		Repository: repository, Worktree: absoluteRoot, Branch: branch,
		HeadCommit: head, BaseRef: baseRef, BaseCommit: base,
		IncludesUncommitted: uncommitted, WorkingTreeDigest: digest,
		OverlayID: repository + "-" + digest[:16], Files: files,
		PlannerElapsedMS: float64(time.Since(started).Microseconds()) / 1000,
	}, nil
}

func stage(plan Plan, directory string) (int, float64, error) {
	started := time.Now()
	count := 0
	for _, change := range plan.Files {
		if change.Status == "deleted" || !indexable(change.Path, cocoIndexSuffixes) {
			continue
		}
		source := filepath.Join(plan.Worktree, filepath.FromSlash(change.Path))
		destination := filepath.Join(directory, plan.Repository, filepath.FromSlash(change.Path))
		if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
			return 0, 0, err
		}
		input, err := os.Open(source)
		if err != nil {
			continue
		}
		output, err := os.Create(destination)
		if err != nil {
			input.Close()
			return 0, 0, err
		}
		_, copyErr := io.Copy(output, input)
		input.Close()
		output.Close()
		if copyErr != nil {
			return 0, 0, copyErr
		}
		count++
	}
	return count, float64(time.Since(started).Microseconds()) / 1000, nil
}

func main() {
	worktree := flag.String("worktree", ".", "Git worktree root")
	base := flag.String("base", "origin/main", "base Git ref")
	repository := flag.String("repository", "", "repository tag")
	stageDir := flag.String("stage-dir", "", "optional CocoIndex staging directory")
	flag.Parse()
	plan, err := buildPlan(*worktree, *base, *repository)
	if err != nil {
		fmt.Fprintf(os.Stderr, "overlay plan failed: %v\n", err)
		os.Exit(2)
	}
	result := summary{Plan: plan}
	for _, change := range plan.Files {
		if change.Status != "deleted" && indexable(change.Path, codannaSuffixes) {
			result.CodannaFiles++
		}
		if change.Status != "deleted" && indexable(change.Path, cocoIndexSuffixes) {
			result.CocoIndexFiles++
		}
		if change.Status == "deleted" {
			result.Tombstones++
		}
	}
	result.IndexedFiles = result.CocoIndexFiles
	if *stageDir != "" {
		staged, elapsed, stageErr := stage(plan, *stageDir)
		if stageErr != nil {
			fmt.Fprintf(os.Stderr, "overlay staging failed: %v\n", stageErr)
			os.Exit(2)
		}
		result.StagedFiles = staged
		result.StagingElapsedMS = elapsed
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		fmt.Fprintf(os.Stderr, "encode result: %v\n", err)
		os.Exit(2)
	}
}
