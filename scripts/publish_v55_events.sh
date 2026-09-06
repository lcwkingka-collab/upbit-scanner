#!/usr/bin/env bash
set -euo pipefail

REPO="/home/ubuntu/upbit-scanner"
LIVE_ROOT="$REPO/data/live/radar_events"
LOCK_FILE="/tmp/v55-events-publish.lock"

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

git -C "$REPO" fetch origin main

for attempt in 1 2 3; do
  upload_dir="$(mktemp -d /tmp/v55-events-upload.XXXXXX)"
  git -C "$REPO" worktree add --detach "$upload_dir" origin/main >/dev/null
  day_kst="$(TZ=Asia/Seoul date +%Y%m%d)"
  source_file="$LIVE_ROOT/$day_kst/v55_events.jsonl"
  target_dir="$upload_dir/data/live/radar_events/$day_kst"
  target_file="$target_dir/v55_events.jsonl"

  if [[ ! -s "$source_file" ]]; then
    git -C "$REPO" worktree remove --force "$upload_dir" >/dev/null
    exit 0
  fi

  mkdir -p "$target_dir"
  cp "$source_file" "$target_file.tmp"
  mv "$target_file.tmp" "$target_file"

  git -C "$upload_dir" add "data/live/radar_events/$day_kst/v55_events.jsonl"
  if git -C "$upload_dir" diff --cached --quiet; then
    git -C "$REPO" worktree remove --force "$upload_dir" >/dev/null
    exit 0
  fi

  git -C "$upload_dir" -c user.name="upbit-radar-v55" -c user.email="upbit-radar-v55@users.noreply.github.com" commit -m "Update V5.5 radar events $day_kst" >/dev/null
  if git -C "$upload_dir" push origin HEAD:main; then
    git -C "$REPO" worktree remove --force "$upload_dir" >/dev/null
    exit 0
  fi

  git -C "$REPO" worktree remove --force "$upload_dir" >/dev/null
  git -C "$REPO" fetch origin main
  sleep 5
done

echo "V5.5 event upload failed after 3 attempts" >&2
exit 1
