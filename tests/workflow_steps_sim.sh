#!/usr/bin/env bash
# Runs the pull-request job's shell steps, extracted from the workflow, against
# a local bare origin with a stub gh. Driven by tests/test_workflow_steps.py.
# shellcheck disable=SC2015  # "check && ok || bad": ok only echoes, so bad runs exactly when a check fails
set -uo pipefail
: "${SIM_REPO:?}" "${SIM_STEPS:?}" "${SIM_WORK:?}"
S=$SIM_STEPS
R=$SIM_WORK; cd "$R" || exit 1
fresh() { local d; d=$(mktemp -d "$R/case.XXXX"); echo "$d"; }
setup() {
  C=$(fresh); cd "$C" || exit 1
  git clone -q --bare "$SIM_REPO" origin.git
  # A bare clone copies only local branches; make sure main exists even when
  # the checkout running the test has no local main.
  git -C origin.git update-ref refs/heads/main "$(git -C "$SIM_REPO" rev-parse HEAD)"
  git -C origin.git symbolic-ref HEAD refs/heads/main
  git -C origin.git branch -q -D model-watch/auto 2>/dev/null || true
  git clone -q origin.git work
  mkdir -p bin tmp watch-in/candidates
  cat > bin/gh <<'G'
#!/usr/bin/env bash
case "$1 $2" in
  "auth setup-git") exit 0 ;;
  "pr list") echo -n "${FAKE_PR:-}" ;;
  *) echo "gh $*" >> "$GHLOG" ;;
esac
G
  chmod +x bin/gh
  export PATH="$C/bin:$PATH" GHLOG="$C/gh.log" RUNNER_TEMP="$C/tmp" GITHUB_REPOSITORY=michelabboud/genie-registry
  export WATCH_BRANCH=model-watch/auto BOT_EMAIL='41898282+github-actions[bot]@users.noreply.github.com'
  export SKIPPED="" FAKE_PR=""
  cp -r watch-in "$RUNNER_TEMP/"
  printf 'body\n' > "$RUNNER_TEMP/watch-in/pr-body.md"
  cd work || exit 1
}
artifact() { rm -rf "$RUNNER_TEMP/watch-in/candidates"; mkdir -p "$RUNNER_TEMP/watch-in/candidates"; for f in "$@"; do printf '%s\n' "${f#*=}" > "$RUNNER_TEMP/watch-in/candidates/${f%%=*}.md"; done; }
branch_step() { : > "$C/out"; GITHUB_OUTPUT="$C/out" GITHUB_STEP_SUMMARY="$C/sum" bash "$S/branch.sh"; local rc=$?; SHA=$(sed -n 's/^sha=//p' "$C/out"); FOREIGN=$(sed -n 's/^foreign=//p' "$C/out"); export SHA PR=""; return $rc; }
open_step() { git checkout -q main 2>/dev/null; git reset -q --hard origin/main; git clean -qfd; GITHUB_STEP_SUMMARY="$C/sum" bash "$S/open.sh"; }
remote_file() { git -C "$C/origin.git" show "refs/heads/$WATCH_BRANCH:candidates/$1.md" 2>/dev/null; }
ok() { echo "PASS $*"; }; bad() { echo "FAIL $*"; FAILS=$((FAILS+1)); }
FAILS=0

# A: absent branch -> created, PR opened, unknown artifact names not admitted
setup; artifact openai=v1 evil=x
branch_step && [ -z "$SHA" ] && [ "$FOREIGN" = false ] || bad "A branch_step"
open_step >/dev/null 2>&1 || bad "A open_step exit"
[ "$(remote_file openai)" = v1 ] && ! remote_file evil >/dev/null && grep -q "pr create" "$C/gh.log" && ok "A absent branch: pushed, PR created, evil.md not admitted" || bad A

# B: same content again -> not pushed, PR edited
FAKE_PR=7; before=$(git -C "$C/origin.git" rev-parse refs/heads/$WATCH_BRANCH)
branch_step; export PR=7; out=$(open_step 2>&1)
after=$(git -C "$C/origin.git" rev-parse refs/heads/$WATCH_BRANCH)
[ "$before" = "$after" ] && grep -q "not pushed" <<<"$out" && grep -q "pr edit 7" "$C/gh.log" && ok "B unchanged: no push, PR edited" || bad B

# E: skipped vendor carried forward from the branch
artifact openai=v1 xai=x1; FAKE_PR=7; branch_step; export PR=7; open_step >/dev/null 2>&1
artifact openai=v2; export SKIPPED=xai; branch_step; export PR=7; open_step >/dev/null 2>&1
[ "$(remote_file xai)" = x1 ] && [ "$(remote_file openai)" = v2 ] && ok "E skipped xai worksheet carried forward" || bad E
export SKIPPED=""

# D: lease refuses when the branch moved after it was read
artifact openai=v3; branch_step; export PR=7
git clone -q "$C/origin.git" "$C/other" && git -C "$C/other" checkout -q $WATCH_BRANCH && echo moved > "$C/other/candidates/gemini.md" && git -C "$C/other" -c user.email="$BOT_EMAIL" -c user.name=bot add -A && git -C "$C/other" -c user.email="$BOT_EMAIL" -c user.name=bot commit -qm race && git -C "$C/other" push -q origin $WATCH_BRANCH
moved=$(git -C "$C/origin.git" rev-parse refs/heads/$WATCH_BRANCH)
out=$(open_step 2>&1); rc=$?
[ $rc -ne 0 ] && [ "$(git -C "$C/origin.git" rev-parse refs/heads/$WATCH_BRANCH)" = "$moved" ] && grep -q "stale info\|rejected" <<<"$out" && ok "D lease refused the racing push (exit $rc)" || bad "D rc=$rc $out"

# C: human commit on the branch -> foreign
git -C "$C/other" pull -q && echo human >> "$C/other/candidates/openai.md" && git -C "$C/other" -c user.email=29182417+michelabboud@users.noreply.github.com -c user.name=Michel commit -qam human && git -C "$C/other" push -q origin $WATCH_BRANCH
branch_step; [ "$FOREIGN" = true ] && ok "C human commit detected, branch left alone" || bad C

# F: main already holds identical worksheets -> warning, exit 0, nothing pushed
setup; artifact openai=same
mkdir -p candidates && echo same > candidates/openai.md && git -c user.email=a@b -c user.name=a add -A && git -c user.email=a@b -c user.name=a commit -qm "merged by mistake" && git push -q origin main
branch_step; out=$(open_step 2>&1); rc=$?
[ $rc -eq 0 ] && grep -q "already holds" <<<"$out" && ! git -C "$C/origin.git" rev-parse -q --verify refs/heads/$WATCH_BRANCH >/dev/null && ok "F empty commit guarded" || bad "F rc=$rc $out"

# G: ls-remote failure -> the step fails, nothing touched
setup; git remote set-url origin "$C/does-not-exist.git"
out=$(branch_step 2>&1); rc=$?
[ $rc -ne 0 ] && grep -q "ls-remote failed" <<<"$out" && ok "G ls-remote failure fails the step (exit $rc)" || bad "G rc=$rc $out"

# H: symlinks in the artifact are never followed
setup; artifact openai=real
ln -s /etc/hostname "$RUNNER_TEMP/watch-in/candidates/xai.md"
branch_step; open_step >/dev/null 2>&1
[ "$(remote_file openai)" = real ] && ! remote_file xai >/dev/null && ok "H1 symlinked worksheet not admitted" || bad H1
setup; artifact openai=real
mv "$RUNNER_TEMP/watch-in/pr-body.md" "$RUNNER_TEMP/real-body.md"
ln -s "$RUNNER_TEMP/real-body.md" "$RUNNER_TEMP/watch-in/pr-body.md"
branch_step; out=$(open_step 2>&1); rc=$?
[ $rc -ne 0 ] && grep -q "not a regular file" <<<"$out" && ! git -C "$C/origin.git" rev-parse -q --verify "refs/heads/$WATCH_BRANCH" >/dev/null && ! [ -s "$C/gh.log" ] && ok "H2 symlinked pr-body refused, nothing pushed (exit $rc)" || bad "H2 rc=$rc $out"

echo "failures=$FAILS"
[ "$FAILS" -eq 0 ]
