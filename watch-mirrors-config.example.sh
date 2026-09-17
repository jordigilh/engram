#!/usr/bin/env bash
# Copy this file to ~/.engram/watch-mirrors-config.sh and replace the example
# entries with the repositories and branches for the local deployment.
#
# Format: "name|live_clone_path|branch|mirror_path"
WATCH_MIRRORS=(
    # "example|${HOME}/src/example|main|${HOME}/.engram/watch/example"
)

# Optional release mirrors. These are normally code-only inputs for a flow
# that explicitly supports release branches.
RELEASE_LINES=()
RELEASE_WATCH_MIRRORS=()
