#!/bin/sh
# Weekly export of what people added, from the server back to the repository.
#
# Run on the host, in the clone the service is hosted from (README, Hosting):
#
# 1. brings the clone up to date with its remote (fetch, then a rebase of the
#    commits that wait here), so that an update of the code and an export
#    never fight;
# 2. writes kratheos.sqlite, the public file of what people added, into the
#    clone from the live database (`docker compose run --rm maintenance
#    export-layer`: the one moment a container writes in the clone). The file
#    holds no contributor key, no report, nothing moderation hid;
# 3. commits that file, and only that file, when it changed. The same data
#    give the same file byte for byte: a week without a change gives no commit;
# 4. pushes, with the git and the deploy key of the host, when the branch has
#    commits its remote lacks: the one just made, or those an earlier run
#    could not push.
#
# Nothing else is ever committed, whatever else is modified or staged in the
# clone. It can be run twice, or by hand between two runs of cron.
#
# It prints nothing when all went well and ends with status 0. When
# something failed it says what on the standard error and ends with status 1,
# so that cron mails it: the remote away or refusing the key (the commit stays
# in the clone, the next run pushes it), an export refused, a clone that
# cannot be rebased. VERBOSE=1 prints each step.
#
# Schedule (crontab -e of the user who owns the clone and the deploy key):
#
#   MAILTO=someone@example.org
#   PATH=/usr/local/bin:/usr/bin:/bin
#   30 3 * * 1  /srv/kratheos-food-data/ops/export-and-push.sh
#
# Updating the clone does not restart the service: the code it brings is run
# from the next `docker compose restart`.

set -eu

FILE=kratheos.sqlite
FALLBACK_NAME=kratheos-food-data
FALLBACK_EMAIL=kratheos-food-data@localhost

CLONE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$CLONE"

# Never wait for an answer nobody will type.
export GIT_TERMINAL_PROMPT=0
: "${GIT_SSH_COMMAND:=ssh -o BatchMode=yes}"
export GIT_SSH_COMMAND

# Print a step, when asked to.
say() {
    if [ "${VERBOSE:-0}" = 1 ]; then
        echo "export-and-push: $*"
    fi
}

# Print what failed; the status of the script becomes 1.
problem() {
    echo "export-and-push: $*" >&2
    status=1
}

status=0
scratch="$(mktemp -d)"
lock="$(git rev-parse --absolute-git-dir)/kratheos-export.lock"

# Remove the scratch files and the lock, whatever way the script ends.
cleanup() {
    rm -rf "$scratch"
    if [ "${locked:-0}" = 1 ]; then
        rm -rf "$lock"
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# One run at a time. A lock left by a run that was killed is taken over.
if ! mkdir "$lock" 2>/dev/null; then
    other="$(cat "$lock/pid" 2>/dev/null || true)"
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        problem "another run is in progress (process $other): nothing done."
        exit 1
    fi
    rm -rf "$lock"
    mkdir "$lock"
fi
locked=1
echo "$$" > "$lock/pid"

upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"

# The commits are made under the git identity of this account or of the clone;
# an account that has none gets one, rather than a commit that fails.
if [ -z "$(git config user.email || true)" ]; then
    GIT_AUTHOR_NAME="$FALLBACK_NAME"
    GIT_AUTHOR_EMAIL="$FALLBACK_EMAIL"
    GIT_COMMITTER_NAME="$FALLBACK_NAME"
    GIT_COMMITTER_EMAIL="$FALLBACK_EMAIL"
    export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
fi

# 1. Up to date with the remote.
if [ -z "$upstream" ]; then
    say "the branch follows no remote branch: nothing is fetched nor pushed"
elif ! git fetch --quiet 2>"$scratch/fetch"; then
    # Not a failure by itself: with nothing to push, nothing is late.
    say "the remote does not answer: $(tr '\n' ' ' < "$scratch/fetch")"
elif [ "$(git rev-list --count "HEAD..$upstream")" -gt 0 ]; then
    # Nothing waits here: the branch simply moves forward, which files modified
    # by hand do not prevent unless the remote changed the same ones. Commits
    # that wait are replayed on top of the remote.
    if [ "$(git rev-list --count "$upstream..HEAD")" -eq 0 ]; then
        set -- merge --ff-only --quiet "$upstream"
    else
        set -- rebase --quiet "$upstream"
    fi
    if git "$@" >"$scratch/update" 2>&1; then
        say "the clone is up to date with $upstream"
    else
        # Back to where it was: the export below is still committed here.
        git rebase --abort >/dev/null 2>&1 || true
        problem "the clone could not be brought up to date with $upstream:"
        sed 's/^/  /' "$scratch/update" >&2
    fi
fi

# 2. The public file, from the live database.
exported=0
if docker compose run --rm -T maintenance export-layer >"$scratch/export" 2>"$scratch/export.err"; then
    exported=1
    # "<file>: written; 2 products, ...; sha256 ...": the counts, for the commit.
    summary="$(tail -n 1 "$scratch/export" | sed 's/^[^:]*: //')"
    say "$summary"
else
    problem "the export failed, $FILE is left as it was:"
    sed 's/^/  /' "$scratch/export.err" >&2
fi

# 3. That file alone, when it changed.
if [ "$exported" = 0 ]; then
    :
elif [ -n "$(git status --porcelain -- "$FILE")" ]; then
    git add -- "$FILE"
    git commit --quiet \
        -m "data: the Kratheos layer exported on $(date -u +%Y-%m-%d)" -m "$summary" -- "$FILE"
    say "committed $FILE"
else
    say "$FILE did not change: nothing to commit"
fi

# 4. What the remote lacks: this commit, or those an earlier run could not push.
if [ -n "$upstream" ]; then
    waiting="$(git rev-list --count "$upstream..HEAD")"
    if [ "$waiting" -gt 0 ]; then
        if git push --quiet 2>"$scratch/push"; then
            say "pushed $waiting commit(s)"
        else
            problem "git push failed: $waiting commit(s) stay in the clone, the next run pushes them."
            sed 's/^/  /' "$scratch/push" >&2
        fi
    fi
fi

exit "$status"
