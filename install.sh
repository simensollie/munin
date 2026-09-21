#!/usr/bin/env bash
#
# Munin installer.
#
# Why this script exists at all: installing should not require reading the spec
# (spec 15). It checks the tools, puts the CLI, daemon and worker on PATH,
# installs and validates the shell plugin, binds the key, copies the systemd
# unit and creates the data root -- seven steps, each reversible.
#
# Two things it must never do quietly. It never runs sudo: non-interactive sudo
# does not work on this machine, so a missing package is printed as an
# `omarchy pkg add` line for a human to run. And it never edits the keybinding
# file silently: ~/.config/hypr/bindings.lua is a symlink into a dotfiles repo,
# so the installer backs up the real target, writes through the link, and says
# on stdout which tracked file in which repo it just changed.
#
# Enablement of the systemd unit is printed, never run (the dotfiles
# convention), unless --enable is passed explicitly. `--uninstall` removes the
# binaries, plugin, bar entry, keybind and unit, and leaves ~/munin/ where it
# is. Nothing recorded is ever removed by an uninstaller.
#
# Every mutating step prints what it touched, and `--dry-run` prints those
# commands instead of running them, so the whole install can be read before it
# happens. `--prefix-home <dir>` redirects every HOME-relative path, which is
# how the tests exercise these code paths without touching a real home.
#
# Owner: install workstream. Contract: docs/superpowers/specs/2026-09-14-poc-contracts.md, section 13.

set -euo pipefail

# A terminal that did not inherit the graphical session has no XDG_RUNTIME_DIR
# (seen on this machine). Without it pw-dump finds nothing, and hyprctl cannot
# derive its instance signature, so the reload in step 5 fails for a reason
# unrelated to the install. /run/user/<uid> is the standard answer.
if [[ -z ${XDG_RUNTIME_DIR:-} && -d "/run/user/$(id -u)" ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(dirname "$SCRIPT_PATH")"

PLUGIN_ID="local.munin"
UNIT_NAME="munin.service"
# The worker is a second unit, not a second job inside the first: the recorder
# must survive the worker dying, being restarted, or being stopped for an
# afternoon (D20).
WORKER_UNIT_NAME="munin-work.service"
# The widget lives in the centre section, immediately right of the weather. It
# is a state indicator that has to be noticed mid-meeting without being looked
# for, and the right section is a row of tray icons that is scanned, not
# glanced at. It is hidden while idle (spec 9.2), so the slot costs nothing
# when nothing is recording.
#
# The anchor is a widget id rather than an index, because an index means a
# different place on every bar. The cost is that PluginRegistry.barTarget
# *fails* the enable when the anchor is not on the bar (a user who removed the
# weather), so the placement falls back to the section alone -- which the
# registry resolves to that same spot, since omarchy.weather is the centre
# section's own default anchor, and to the end of the section without it.
BAR_SECTION="center"
BAR_ANCHOR="omarchy.weather"
KEYBIND_KEYS="SUPER + SHIFT + R"
KEYBIND_DESC="Record meeting"
KEYBIND_CMD="munin toggle"
# The split (D26) gets the record binding plus CTRL: the two are the same
# gesture at the same moment -- you are at a meeting boundary either way -- and
# a notification that has to be caught within a minute is not the only way in.
# The panel button covers the same ground for a mouse.
SPLIT_KEYBIND_KEYS="SUPER + CTRL + SHIFT + R"
SPLIT_KEYBIND_DESC="Split meeting here"
SPLIT_KEYBIND_CMD="munin split --now"
MARK_BEGIN="-- >>> munin (managed by munin install.sh) >>>"
MARK_END="-- <<< munin (managed by munin install.sh) <<<"

DRY_RUN=0
DO_UNINSTALL=0
DO_ENABLE=0
PREFIX_HOME=""

usage() {
  cat <<'USAGE'
Usage: install.sh [--dry-run] [--enable] [--prefix-home <dir>]
       install.sh --uninstall [--dry-run] [--prefix-home <dir>]

  --dry-run           print every mutating command instead of running it
  --enable            also run: systemctl --user enable --now munin.service munin-work.service
  --prefix-home <dir> treat <dir> as the home directory (tests only)
  --uninstall         reverse steps 2-6; leaves the data root in place
USAGE
}

while (($# > 0)); do
  case "$1" in
  --dry-run) DRY_RUN=1 ;;
  --uninstall) DO_UNINSTALL=1 ;;
  --enable) DO_ENABLE=1 ;;
  --prefix-home)
    if [[ -z ${2:-} ]]; then
      echo "install.sh: --prefix-home needs a directory" >&2
      exit 2
    fi
    PREFIX_HOME="$2"
    shift
    ;;
  --prefix-home=*) PREFIX_HOME="${1#*=}" ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "install.sh: unknown argument: $1" >&2
    usage >&2
    exit 2
    ;;
  esac
  shift
done

HOME_DIR="${PREFIX_HOME:-$HOME}"
BIN_DIR="$HOME_DIR/.local/bin"
VENV_DIR="$HOME_DIR/.local/share/munin/venv"
PLUGIN_ROOT="$HOME_DIR/.config/omarchy/plugins"
PLUGIN_DIR="$PLUGIN_ROOT/$PLUGIN_ID"
UNIT_DIR="$HOME_DIR/.config/systemd/user"
BINDINGS_LINK="$HOME_DIR/.config/hypr/bindings.lua"
SHELL_JSON="$HOME_DIR/.config/omarchy/shell.json"
# Munin's own state, and where the keybinding backup goes. Not next to the file
# it backs up: that file is a symlink into somebody's dotfiles repository, and a
# stray `bindings.lua.munin-bak` there is untracked litter an uninstall would
# never reach and a `git add -A` would happily commit. $XDG_STATE_HOME is
# ignored under --prefix-home so the tests stay inside their own tmp directory.
if [[ -n $PREFIX_HOME ]]; then
  STATE_DIR="$HOME_DIR/.local/state/munin"
else
  STATE_DIR="${XDG_STATE_HOME:-$HOME_DIR/.local/state}/munin"
fi
DATA_HOME="${MUNIN_HOME:-$HOME_DIR/munin}"
OMARCHY_DEFAULT_BINDINGS="${OMARCHY_DEFAULT_BINDINGS:-/usr/share/omarchy/default/hypr/bindings}"

# ---------------------------------------------------------------------------
# Output and command plumbing. Every mutation goes through run/run_append so
# that --dry-run is one honest switch rather than seven special cases.
# ---------------------------------------------------------------------------

say() { printf '%s\n' "$*"; }

# In a dry run the "what it touched" lines are marked, so the summary can never
# be mistaken for a report of something that actually happened.
info() {
  if ((DRY_RUN)); then
    printf '  (dry run) %s\n' "$*"
  else
    printf '  %s\n' "$*"
  fi
}

warn() { printf '  warning: %s\n' "$*" >&2; }

step() { printf '\n[%d/7] %s\n' "$1" "$2"; }

fail() {
  printf 'install.sh: step %s failed: %s\n' "$1" "$2" >&2
  exit "$1"
}

run() {
  if ((DRY_RUN)); then
    printf '  would run: %s\n' "$*"
    return 0
  fi
  "$@"
}

# Append stdin to a file, following a symlink so a dotfiles repo sees the edit.
run_append() {
  local path="$1"
  if ((DRY_RUN)); then
    printf '  would append to: %s\n' "$path"
    cat >/dev/null
    return 0
  fi
  cat >>"$path"
}

# Replace a file's contents with stdin, following a symlink like run_append.
# Only ever fed a fully buffered string: writing through the link truncates it,
# so a caller that streamed the same file into this would eat it.
run_write() {
  local path="$1"
  if ((DRY_RUN)); then
    printf '  would rewrite: %s\n' "$path"
    cat >/dev/null
    return 0
  fi
  cat >"$path"
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Step 1: tools. Read-only, and it runs under --dry-run too, because a dry run
# that does not tell you what is missing is worth nothing.
# ---------------------------------------------------------------------------

MISSING_PKGS=()

need_cmd() { # need_cmd <command> <arch package>
  if ! have "$1"; then
    warn "missing: $1"
    MISSING_PKGS+=("$2")
    return 1
  fi
  info "found $1 ($(command -v "$1"))"
}

step_check() {
  step 1 "Checking required tools"
  local ok=1
  need_cmd python3 python || ok=0
  need_cmd ffmpeg ffmpeg || ok=0
  need_cmd pw-record pipewire || ok=0
  need_cmd pw-dump pipewire || ok=0
  need_cmd hyprctl hyprland || ok=0

  if have python3; then
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
      info "python3 is $(python3 --version 2>&1)"
    else
      warn "python3 is $(python3 --version 2>&1); munin needs 3.12 or newer"
      ok=0
    fi
  fi

  if have omarchy; then
    info "found omarchy ($(command -v omarchy))"
  else
    warn "missing: omarchy -- this installer targets an Omarchy 4 desktop"
    ok=0
  fi

  if ((${#MISSING_PKGS[@]})); then
    local uniq
    uniq="$(printf '%s\n' "${MISSING_PKGS[@]}" | sort -u | tr '\n' ' ')"
    say ""
    say "Install the missing packages yourself (this script never runs sudo):"
    say "  omarchy pkg add ${uniq% }"
  fi

  ((ok)) || fail 1 "required tools are missing"
}

# ---------------------------------------------------------------------------
# Step 2: the venv and the three entry points. A venv rather than --user so an
# uninstall is one directory, and symlinks rather than copies so an editable
# reinstall does not need the installer run again.
# ---------------------------------------------------------------------------

step_venv() {
  step 2 "Installing into $VENV_DIR"
  run mkdir -p "$(dirname "$VENV_DIR")"
  if [[ -x $VENV_DIR/bin/python ]]; then
    info "venv already present, refreshing the package"
  else
    run python3 -m venv "$VENV_DIR" || fail 2 "could not create the venv"
    info "created $VENV_DIR"
  fi
  run "$VENV_DIR/bin/pip" install --quiet --editable "$REPO_DIR" ||
    fail 2 "pip could not install $REPO_DIR"
  info "installed munin (editable) from $REPO_DIR"

  run mkdir -p "$BIN_DIR"
  local s
  for s in munin munin-rec munin-work; do
    run ln -sfn "$VENV_DIR/bin/$s" "$BIN_DIR/$s" || fail 2 "could not link $BIN_DIR/$s"
    info "linked $BIN_DIR/$s -> $VENV_DIR/bin/$s"
  done
  case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on PATH; the keybind and the plugin call munin by name" ;;
  esac
}

# ---------------------------------------------------------------------------
# Step 3: the shell plugin, copied (never symlinked -- the validator rejects
# symlinks anywhere inside a plugin folder) and validated where it will run.
# ---------------------------------------------------------------------------

# Set by step 3 when an already-installed plugin is replaced by different
# files. The shell hot-reloads a changed plugin folder, but that reload keeps
# the JavaScript module (Model.js) it already evaluated -- measured: a new
# Model.js only took effect after `omarchy-restart-shell`. So step 4 restarts
# the shell when the plugin changed. The recording daemon is untouched by that
# (D20; verified: munin.service stayed active through the restart).
PLUGIN_CHANGED=0

step_plugin_copy() {
  step 3 "Installing the shell plugin"
  local src="$REPO_DIR/plugin/$PLUGIN_ID"
  [[ -d $src ]] || fail 3 "plugin source is missing: $src"
  if [[ -d $PLUGIN_DIR ]] && ! diff -rq "$src" "$PLUGIN_DIR" >/dev/null 2>&1; then
    PLUGIN_CHANGED=1
    info "the installed plugin differs from the repository copy; it will be replaced"
  fi
  run mkdir -p "$PLUGIN_ROOT"
  run rm -rf "$PLUGIN_DIR"
  run cp -R "$src" "$PLUGIN_DIR" || fail 3 "could not copy the plugin"
  info "copied $src -> $PLUGIN_DIR"
  if have omarchy; then
    run omarchy plugin validate "$PLUGIN_DIR" ||
      fail 3 "omarchy plugin validate rejected $PLUGIN_DIR"
    info "validated $PLUGIN_DIR"
  else
    warn "omarchy is missing; skipped plugin validation"
  fi
}

# ---------------------------------------------------------------------------
# Step 4: make the running shell see it. rescanPlugins first, because enable
# fails on an id the live registry has never heard of.
# ---------------------------------------------------------------------------

bar_has_plugin() {
  [[ -f $SHELL_JSON ]] || return 1
  have jq || return 1
  local found
  found="$(jq -r --arg id "$PLUGIN_ID" \
    '[.bar.layout[]?[]? | select(.id == $id)] | length' "$SHELL_JSON" 2>/dev/null || echo 0)"
  [[ $found != "0" ]]
}

# Everything in this step needs a *running* Omarchy shell: `omarchy plugin
# enable` talks to it over a socket and says so itself ("the shell is expected
# to already be running; this command does not start it"). Installing over ssh,
# from a bare TTY, or before the first graphical login is ordinary, and it must
# not abort the install: steps 5-7 (keybind, unit, data root) are filesystem
# work that has nothing to do with the shell, and stranding a half-install with
# no data root is worse than a plugin that is enabled by hand a minute later.
shell_step_deferred() {
  warn "$1"
  warn "the Omarchy shell is not reachable; the plugin is installed but not enabled"
  say "  Once the shell is running, finish this step yourself with:"
  say "      omarchy plugin enable $PLUGIN_ID --section $BAR_SECTION --after $BAR_ANCHOR"
  return 0
}

# `omarchy plugin enable` is answered by the running shell, which persists its
# *in-memory* configuration back to shell.json (shell.qml: mutateShellConfig
# copies shellConfig and writes it). Seen on this machine: that flipped
# bar.transparent from false to true and made the bar unreadable, because the
# shell's idea of the bar differed from the file. Snapshot the two settings a
# user notices and put them back if the enable changed them.
bar_setting() {
  [[ -f $SHELL_JSON ]] && have jq || { echo ""; return 0; }
  # Not `//`: jq treats false like null, and false is the value that matters.
  jq -r --arg key "$1" 'if .bar[$key] == null then "" else (.bar[$key] | tostring) end' \
    "$SHELL_JSON" 2>/dev/null || echo ""
}

restore_bar_settings() {
  local before_transparent="$1" before_position="$2" after
  after="$(bar_setting transparent)"
  if [[ -n $before_transparent && $after != "$before_transparent" ]]; then
    warn "the shell changed bar.transparent $before_transparent -> $after while enabling; restoring"
    run omarchy bar transparent "$before_transparent" || warn "could not restore bar transparency"
  fi
  after="$(bar_setting position)"
  if [[ -n $before_position && $after != "$before_position" ]]; then
    warn "the shell changed bar.position $before_position -> $after while enabling; restoring"
    run omarchy bar position "$before_position" || warn "could not restore bar position"
  fi
}

step_plugin_enable() {
  step 4 "Enabling the plugin and placing it on the bar"
  if ! have omarchy; then
    warn "omarchy is missing; skipped enable and bar placement"
    return 0
  fi
  local before_transparent before_position
  before_transparent="$(bar_setting transparent)"
  before_position="$(bar_setting position)"
  if have omarchy-shell; then
    run omarchy-shell shell rescanPlugins || warn "rescanPlugins failed; is the shell running?"
  fi
  # The placement has to ride along with `enable`. Enabling a plugin that
  # declares a bar-widget already puts it on the bar, at the registry's default
  # anchor for the manifest's section, and a `bar put` afterwards is a no-op on
  # a widget that is already placed. So this call is the only moment the
  # position can be chosen at all. A user who moves it later keeps that move:
  # the branch below never re-places a widget that is already in the layout.
  if bar_has_plugin; then
    info "$PLUGIN_ID is already on the bar; left where it is"
    run omarchy plugin enable "$PLUGIN_ID" ||
      { shell_step_deferred "omarchy plugin enable $PLUGIN_ID failed"; return 0; }
  else
    if run omarchy plugin enable "$PLUGIN_ID" --section "$BAR_SECTION" --after "$BAR_ANCHOR"; then
      info "placed $PLUGIN_ID on the bar in $BAR_SECTION, after $BAR_ANCHOR"
    elif run omarchy plugin enable "$PLUGIN_ID" --section "$BAR_SECTION"; then
      # No weather widget to sit beside. The section is still right, so say
      # where it actually landed rather than claiming the anchor.
      warn "$BAR_ANCHOR is not on the bar; placed $PLUGIN_ID in $BAR_SECTION at the section default"
    else
      shell_step_deferred "omarchy plugin enable $PLUGIN_ID --section $BAR_SECTION --after $BAR_ANCHOR failed"
      return 0
    fi
  fi
  info "enabled $PLUGIN_ID"
  (( DRY_RUN )) || restore_bar_settings "$before_transparent" "$before_position"
  restart_shell_if_plugin_changed
}

restart_shell_if_plugin_changed() {
  (( PLUGIN_CHANGED )) || return 0
  if ! have omarchy-restart-shell; then
    warn "the plugin changed but omarchy-restart-shell is missing; restart the shell yourself"
    return 0
  fi
  if have omarchy-shell && ! omarchy-shell -q shell ping >/dev/null 2>&1; then
    info "the plugin changed; the shell is not running, so the next start loads it"
    return 0
  fi
  info "the plugin changed; a hot-reload keeps the old Model.js, so the shell is restarted"
  run omarchy-restart-shell || warn "omarchy-restart-shell failed; restart the shell yourself"
}

# ---------------------------------------------------------------------------
# Step 5: the keybind. The loudest step in the script on purpose: the file is a
# symlink into a git repository somebody else maintains.
# ---------------------------------------------------------------------------

resolve_bindings_target() {
  if [[ -L $BINDINGS_LINK ]]; then
    readlink -f "$BINDINGS_LINK"
  else
    printf '%s\n' "$BINDINGS_LINK"
  fi
}

key_bound_by_omarchy() {
  local keys="$1"
  [[ -d $OMARCHY_DEFAULT_BINDINGS ]] || return 1
  grep -rqF -e "\"$keys\"" "$OMARCHY_DEFAULT_BINDINGS" 2>/dev/null
}

# The managed block, markers included. One writer for both paths -- appending it
# to a file that has none, and rewriting one that predates the split binding --
# so the two cannot describe different keybindings.
munin_block() {
  printf '%s\n' "$MARK_BEGIN"
  local keys
  for keys in "$KEYBIND_KEYS" "$SPLIT_KEYBIND_KEYS"; do
    if key_bound_by_omarchy "$keys"; then
      printf 'hl.unbind("%s")\n' "$keys"
    fi
  done
  printf 'o.bind("%s", "%s", "%s")\n' "$KEYBIND_KEYS" "$KEYBIND_DESC" "$KEYBIND_CMD"
  printf 'o.bind("%s", "%s", "%s")\n' \
    "$SPLIT_KEYBIND_KEYS" "$SPLIT_KEYBIND_DESC" "$SPLIT_KEYBIND_CMD"
  printf '%s\n' "$MARK_END"
}

# Timestamped, and a fresh one every time the block is actually written: a
# backup kept from an earlier install is a snapshot of a file edited many times
# since, so restoring from it would revert somebody else's work.
backup_bindings() {
  local target="$1" backup
  backup="$STATE_DIR/$(basename "$target").$(date +%Y-%m-%dT%H%M%S).bak"
  run mkdir -p "$STATE_DIR"
  run cp -p "$target" "$backup" || fail 5 "could not back up $target"
  info "backed up $target -> $backup"
}

# Swap an older managed block for the current one, in place. Buffered whole
# before anything is written: the file being read is the file being written.
rewrite_munin_block() {
  local target="$1" rebuilt line inblock=0
  rebuilt="$(
    while IFS= read -r line || [[ -n $line ]]; do
      if [[ $line == "$MARK_BEGIN" ]]; then
        inblock=1
        munin_block
        continue
      fi
      if [[ $line == "$MARK_END" ]]; then
        inblock=0
        continue
      fi
      ((inblock)) || printf '%s\n' "$line"
    done <"$target"
  )"
  printf '%s\n' "$rebuilt" | run_write "$BINDINGS_LINK"
}

reload_hypr() {
  if ! have hyprctl; then
    warn "hyprctl is missing; reload Hyprland yourself"
    return 0
  fi
  if [[ -z ${HYPRLAND_INSTANCE_SIGNATURE:-} && -d ${XDG_RUNTIME_DIR:-/nonexistent}/hypr ]]; then
    local sig
    sig="$(find "${XDG_RUNTIME_DIR}/hypr" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | head -n 1 || true)"
    if [[ -n $sig ]]; then
      export HYPRLAND_INSTANCE_SIGNATURE="$sig"
    fi
  fi
  run hyprctl reload || warn "hyprctl reload failed; reload Hyprland yourself"
  run hyprctl configerrors || true
}

step_keybind() {
  step 5 "Binding $KEYBIND_KEYS to '$KEYBIND_CMD' and $SPLIT_KEYBIND_KEYS to '$SPLIT_KEYBIND_CMD'"
  local target
  target="$(resolve_bindings_target)"

  if [[ ! -e $target ]]; then
    run mkdir -p "$(dirname "$BINDINGS_LINK")"
    run touch "$BINDINGS_LINK"
    info "created $BINDINGS_LINK (it did not exist)"
  fi

  say "  keybinding file: $BINDINGS_LINK"
  if [[ $target != "$BINDINGS_LINK" ]]; then
    say "  which is a SYMLINK to: $target"
    say "  that file is tracked in another repository -- this edit will show up in its git status"
  fi

  if [[ -e $target ]] && grep -qF -e "$MARK_BEGIN" "$target" 2>/dev/null; then
    if grep -qF -e "\"$SPLIT_KEYBIND_KEYS\"" "$target" 2>/dev/null; then
      info "the munin block is already present; left unchanged"
      reload_hypr
      return 0
    fi
    # An install from before the split binding existed. The block is ours and
    # it is one binding short, so it is rewritten rather than appended to --
    # two munin blocks in one file is how a keybinding ends up bound twice.
    backup_bindings "$target" || return 1
    rewrite_munin_block "$target"
    info "the munin block predates $SPLIT_KEYBIND_KEYS; rewrote it with both bindings"
    reload_hypr
    return 0
  fi

  local keys
  for keys in "$KEYBIND_KEYS" "$SPLIT_KEYBIND_KEYS"; do
    if [[ -e $target ]] && grep -qF -e "\"$keys\"" "$target" 2>/dev/null; then
      warn "$keys is already bound in $target; not touching it"
      warn "bind munin yourself with the two o.bind lines in install.sh"
      return 0
    fi
  done

  backup_bindings "$target" || return 1

  {
    printf '\n'
    munin_block
  } | run_append "$BINDINGS_LINK"
  info "appended the munin keybind block through $BINDINGS_LINK"

  reload_hypr
}

# ---------------------------------------------------------------------------
# Step 6: the unit. Copied, never enabled -- enablement symlinks are a machine
# decision, not a repository one.
# ---------------------------------------------------------------------------

step_unit() {
  step 6 "Installing the systemd user units"
  run mkdir -p "$UNIT_DIR"
  local unit
  for unit in "$UNIT_NAME" "$WORKER_UNIT_NAME"; do
    run cp "$REPO_DIR/systemd/$unit" "$UNIT_DIR/$unit" || fail 6 "could not copy $unit"
    info "copied $REPO_DIR/systemd/$unit -> $UNIT_DIR/$unit"
  done
  run systemctl --user daemon-reload || warn "systemctl --user daemon-reload failed"
  if ((DO_ENABLE)); then
    for unit in "$UNIT_NAME" "$WORKER_UNIT_NAME"; do
      run systemctl --user enable --now "$unit" || fail 6 "could not enable $unit"
      info "enabled and started $unit"
    done
  else
    say ""
    say "  The units are installed but NOT enabled. Start them yourself with:"
    say "      systemctl --user enable --now $UNIT_NAME $WORKER_UNIT_NAME"
  fi
}

# ---------------------------------------------------------------------------
# Step 7: the data root. The config is written only when absent: a config is
# hand-edited, and an installer that clobbers it is one people stop running.
# ---------------------------------------------------------------------------

step_data_root() {
  step 7 "Creating the data root"
  run mkdir -p "$DATA_HOME/recordings" "$DATA_HOME/inbox" "$DATA_HOME/voices"
  info "data root: $DATA_HOME (recordings/, inbox/, voices/)"
  if [[ -f $DATA_HOME/config.toml ]]; then
    info "config.toml already exists; left untouched"
  else
    run env "MUNIN_HOME=$DATA_HOME" "$VENV_DIR/bin/munin" setup --write-default-config ||
      fail 7 "munin setup --write-default-config failed"
    info "wrote $DATA_HOME/config.toml"
  fi
}

# ---------------------------------------------------------------------------
# Uninstall: the reverse of 2-6, in reverse order, and never the data root.
# ---------------------------------------------------------------------------

uninstall_unit() {
  say ""
  say "Removing the systemd user units"
  local unit
  local removed=0
  for unit in "$UNIT_NAME" "$WORKER_UNIT_NAME"; do
    if [[ -f $UNIT_DIR/$unit ]]; then
      run systemctl --user disable --now "$unit" || warn "could not disable $unit"
      run rm -f "$UNIT_DIR/$unit"
      info "removed $UNIT_DIR/$unit"
      removed=1
    else
      info "no unit at $UNIT_DIR/$unit"
    fi
  done
  ((removed)) && run systemctl --user daemon-reload || true
}

uninstall_keybind() {
  say ""
  say "Removing the keybind block"
  local target
  target="$(resolve_bindings_target)"
  if [[ ! -e $target ]]; then
    info "no keybinding file at $target"
    return 0
  fi
  if ! grep -qF -e "$MARK_BEGIN" "$target"; then
    info "no munin block in $target"
    return 0
  fi
  say "  editing: $BINDINGS_LINK"
  if [[ $target != "$BINDINGS_LINK" ]]; then
    say "  which is a SYMLINK to: $target"
  fi
  if ((DRY_RUN)); then
    printf '  would remove the munin block from: %s\n' "$target"
    return 0
  fi
  local tmp
  tmp="$(mktemp "$target.munin-tmp.XXXXXX")"
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    index($0, b) { skip = 1 }
    !skip { print }
    index($0, e) { skip = 0 }
  ' "$target" >"$tmp"
  cat "$tmp" >"$target"
  rm -f "$tmp"
  info "removed the munin block from $target"

  # The block is gone, so the backups have nothing left to restore. Includes the
  # pre-1.0 in-place backup, which earlier installs left inside the dotfiles
  # repository itself.
  local stale
  for stale in "$STATE_DIR/$(basename "$target")".*.bak "$target.munin-bak"; do
    [[ -e $stale ]] || continue
    rm -f "$stale"
    info "removed the backup $stale"
  done

  # Hyprland still holds the binding until it is told otherwise, and the same
  # uninstall is about to delete the `munin` binary it points at.
  reload_hypr
}

uninstall_plugin() {
  say ""
  say "Removing the shell plugin"
  if have omarchy; then
    run omarchy plugin disable "$PLUGIN_ID" || warn "omarchy plugin disable failed"
  fi
  if [[ -f $SHELL_JSON ]] && have jq && bar_has_plugin; then
    if ((DRY_RUN)); then
      printf '  would remove %s from the bar layout in %s\n' "$PLUGIN_ID" "$SHELL_JSON"
    else
      cp -p "$SHELL_JSON" "$SHELL_JSON.munin-bak"
      info "backed up $SHELL_JSON -> $SHELL_JSON.munin-bak"
      local tmp
      tmp="$(mktemp "$SHELL_JSON.munin-tmp.XXXXXX")"
      jq --arg id "$PLUGIN_ID" \
        '.bar.layout |= with_entries(.value |= map(select(.id != $id)))' \
        "$SHELL_JSON" >"$tmp"
      mv "$tmp" "$SHELL_JSON"
      info "removed $PLUGIN_ID from the bar layout in $SHELL_JSON"
    fi
  fi
  if [[ -d $PLUGIN_DIR ]]; then
    run rm -rf "$PLUGIN_DIR"
    info "removed $PLUGIN_DIR"
  else
    info "no plugin at $PLUGIN_DIR"
  fi
  if have omarchy-shell; then
    run omarchy-shell shell rescanPlugins || true
  fi
}

uninstall_venv() {
  say ""
  say "Removing the entry points and the venv"
  local s
  for s in munin munin-rec munin-work; do
    if [[ -L $BIN_DIR/$s || -e $BIN_DIR/$s ]]; then
      run rm -f "$BIN_DIR/$s"
      info "removed $BIN_DIR/$s"
    fi
  done
  if [[ -d $VENV_DIR ]]; then
    run rm -rf "$VENV_DIR"
    info "removed $VENV_DIR"
  else
    info "no venv at $VENV_DIR"
  fi
}

do_uninstall() {
  say "Uninstalling munin (the data root is never touched)"
  uninstall_unit
  uninstall_keybind
  uninstall_plugin
  uninstall_venv
  say ""
  say "Your recordings are untouched: $DATA_HOME"
  say "Delete that directory yourself if you really want the audio gone."
}

do_install() {
  say "Installing munin from $REPO_DIR"
  if ((DRY_RUN)); then
    say "(dry run: nothing will be changed)"
  fi
  step_check
  step_venv
  step_plugin_copy
  step_plugin_enable
  step_keybind
  step_unit
  step_data_root
  say ""
  say "Done. Next:"
  say "  munin doctor            check the install"
  say "  munin setup             pick a microphone and test the two tracks"
  if ((DO_ENABLE)); then
    :
  else
    say "  systemctl --user enable --now $UNIT_NAME $WORKER_UNIT_NAME"
  fi
}

if ((DO_UNINSTALL)); then
  do_uninstall
else
  do_install
fi
