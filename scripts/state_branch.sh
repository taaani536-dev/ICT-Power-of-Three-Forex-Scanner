#!/usr/bin/env bash
# Durable state on a dedicated git branch ("scanner-state"), checked out as a worktree in .state/.
#   state_branch.sh prepare   -> make .state/ available (creates the orphan branch on first run)
#   state_branch.sh persist   -> commit .state/state.json and push, merging on conflict
set -euo pipefail

BRANCH="${STATE_BRANCH:-scanner-state}"
DIR="${STATE_DIR:-.state}"
FILE="state.json"
MAX_ATTEMPTS=6

cmd="${1:-}"

git_identity() {
  git config user.name  "forex-scanner-bot"
  git config user.email "forex-scanner-bot@users.noreply.github.com"
}

prepare() {
  git_identity
  if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    git fetch --no-tags origin "$BRANCH"
    git worktree add -B "$BRANCH" "$DIR" "origin/$BRANCH"
    echo "State branch '$BRANCH' checked out into $DIR"
  else
    echo "State branch '$BRANCH' does not exist yet - creating it (first run)"
    git worktree add --detach "$DIR"
    ( cd "$DIR" && git checkout --orphan "$BRANCH" && git rm -rf . >/dev/null 2>&1 || true )
    printf 'State storage for the forex scanner. Do not edit by hand.\n' > "$DIR/README.md"
  fi
}

persist() {
  git_identity
  local root; root="$(pwd)"
  [ -d "$DIR" ] || { echo "No $DIR directory - nothing to persist"; exit 1; }
  cd "$DIR"
  [ -f "$FILE" ] || { echo "No $FILE produced - nothing to persist"; exit 0; }
  git add -A
  if git diff --cached --quiet; then
    echo "State unchanged - nothing to commit"
    return 0
  fi
  git commit -q -m "state: $(date -u +%Y-%m-%dT%H:%M:%SZ) [skip ci]"
  local remote_copy; remote_copy="$(mktemp)"
  for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
    if git push -q origin "HEAD:refs/heads/$BRANCH"; then
      echo "State pushed (attempt $attempt)"
      rm -f "$remote_copy"
      return 0
    fi
    echo "Push rejected (attempt $attempt) - fetching remote state and merging"
    git fetch -q --no-tags origin "$BRANCH"
    if git show "origin/$BRANCH:$FILE" > "$remote_copy" 2>/dev/null; then
      python "$root/main.py" merge-state --ours "$FILE" --theirs "$remote_copy" --output "$FILE.merged"
      mv "$FILE.merged" "$FILE"
    fi
    git reset -q --soft "origin/$BRANCH"
    git add -A
    git commit -q -m "state: merged $(date -u +%Y-%m-%dT%H:%M:%SZ) [skip ci]" || true
    sleep "${PUSH_RETRY_SLEEP:-$((attempt * 3))}"
  done
  rm -f "$remote_copy"
  echo "ERROR: could not push state after $MAX_ATTEMPTS attempts" >&2
  return 1
}

case "$cmd" in
  prepare) prepare ;;
  persist) persist ;;
  *) echo "usage: $0 prepare|persist" >&2; exit 2 ;;
esac
