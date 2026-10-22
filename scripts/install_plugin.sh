#!/usr/bin/env bash
# Headless install of the brieflow-auto plugin into a user's Claude Code state.
#
# Intended for VM bootstrap (where the user lands on the VM and needs the plugin
# already present — no slash-command intervention possible). Also useful for
# CI, dev containers, and any non-interactive install.
#
# Sources (mutually exclusive):
#   --git [--ref REF]      Clone cheeseman-lab/brieflow-auto (optionally pin REF)
#   --tarball PATH         Use a brieflow-auto-plugin.tgz with brieflow-auto/ at its top level
#   --dir PATH             Use an existing checkout / extracted marketplace dir
#
# Modifiers:
#   --dev                  (requires --dir) Symlink the cache to the source plugin
#                          directory instead of copying. Edits to the source are
#                          live for the runner — no reinstall between changes.
#                          Also flips autoUpdate=false on the marketplace entry
#                          so a sync doesn't clobber the symlink with a copy.
#
# After running, the plugin appears in ~/.claude/plugins/installed_plugins.json
# and is loaded the next time Claude Code starts (or on /reload-plugins in an
# already-open session).
#
# Exit 0 on success, nonzero on any error. Idempotent — re-running replaces
# the prior install of brieflow-auto cleanly.

set -euo pipefail

PLUGIN_NAME="brieflow-auto"
MARKETPLACE_NAME="brieflow-auto"                     # unified name across git and tarball installs
REPO_DEFAULT="cheeseman-lab/brieflow-auto"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
PLUGINS_DIR="$CLAUDE_HOME/plugins"

source_kind=""
source_path=""
git_ref=""
dev_mode=false

die() { echo "install_plugin: $*" >&2; exit 1; }
log() { echo "install_plugin: $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --git)       source_kind="git"; shift ;;
    --ref)       git_ref="$2"; shift 2 ;;
    --tarball)   source_kind="tarball"; source_path="$2"; shift 2 ;;
    --dir)       source_kind="dir"; source_path="$2"; shift 2 ;;
    --dev)       dev_mode=true; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done

[[ -n "$source_kind" ]] || die "must specify --git, --tarball PATH, or --dir PATH"
[[ "$dev_mode" == "true" && "$source_kind" != "dir" ]] && die "--dev requires --dir (symlinks only make sense for a local checkout)"
command -v python3 >/dev/null || die "python3 is required"

mkdir -p "$PLUGINS_DIR/marketplaces" "$PLUGINS_DIR/cache"

# --- Resolve source → a marketplace directory on disk -----------------------
marketplace_dir=""
marketplace_name=""

case "$source_kind" in
  git)
    command -v git >/dev/null || die "git is required for --git"
    marketplace_dir="$PLUGINS_DIR/marketplaces/$MARKETPLACE_NAME"
    marketplace_name="$MARKETPLACE_NAME"
    if [[ -d "$marketplace_dir/.git" && -n "$git_ref" ]]; then
      [[ -z "$(git -C "$marketplace_dir" status --porcelain)" ]] || \
        die "existing clone at $marketplace_dir has local changes; cannot check out $git_ref"
      log "updating existing clone at $marketplace_dir to $git_ref"
      git -C "$marketplace_dir" fetch --quiet --tags origin
      git -C "$marketplace_dir" checkout --quiet "$git_ref"
    elif [[ -d "$marketplace_dir/.git" ]]; then
      log "reusing existing clone at $marketplace_dir (no --ref: no git ops — manage manually)"
    else
      log "cloning $REPO_DEFAULT into $marketplace_dir"
      git clone --quiet "https://github.com/$REPO_DEFAULT" "$marketplace_dir"
      [[ -n "$git_ref" ]] && git -C "$marketplace_dir" checkout --quiet "$git_ref"
    fi
    ;;
  tarball)
    [[ -f "$source_path" ]] || die "tarball not found: $source_path"
    marketplace_dir="$PLUGINS_DIR/marketplaces/$MARKETPLACE_NAME"
    marketplace_name="$MARKETPLACE_NAME"
    log "extracting tarball $source_path → $marketplace_dir"
    rm -rf "$marketplace_dir"
    mkdir -p "$marketplace_dir/plugins" "$marketplace_dir/.claude-plugin"
    tar -xzf "$source_path" -C "$marketplace_dir/plugins/"
    [[ -d "$marketplace_dir/plugins/$PLUGIN_NAME" ]] || \
      die "tarball did not contain $PLUGIN_NAME/ at top level"
    # Synthesize a marketplace.json wrapping the extracted plugin.
    python3 - "$marketplace_dir" "$marketplace_name" "$PLUGIN_NAME" <<'PY'
import json, sys, pathlib
md, mname, pname = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
plug_json = json.loads((md / "plugins" / pname / ".claude-plugin" / "plugin.json").read_text())
(md / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
    "name": mname,
    "owner": {"name": "synthetic (tarball install)"},
    "metadata": {"description": f"Synthetic marketplace wrapping {pname} from a tarball install.",
                 "version": plug_json.get("version", "0.0.0")},
    "plugins": [{"name": pname, "source": f"./plugins/{pname}",
                 "version": plug_json.get("version", "0.0.0"),
                 "description": plug_json.get("description", "")}]
}, indent=2) + "\n")
PY
    ;;
  dir)
    [[ -d "$source_path/.claude-plugin" ]] || \
      die "$source_path is not a marketplace dir (missing .claude-plugin/)"
    marketplace_dir="$source_path"
    marketplace_name=$(python3 -c \
      "import json,sys; print(json.load(open('$source_path/.claude-plugin/marketplace.json'))['name'])")
    ;;
esac

# --- Read plugin version from plugin.json -----------------------------------
plugin_dir="$marketplace_dir/plugins/$PLUGIN_NAME"
[[ -f "$plugin_dir/.claude-plugin/plugin.json" ]] || die "plugin.json not found at $plugin_dir/.claude-plugin/plugin.json"
plugin_version=$(python3 -c \
  "import json; print(json.load(open('$plugin_dir/.claude-plugin/plugin.json'))['version'])")

# --- Populate cache (this is what Claude Code actually loads from) ----------
cache_dir="$PLUGINS_DIR/cache/$marketplace_name/$PLUGIN_NAME/$plugin_version"
mkdir -p "$(dirname "$cache_dir")"
if [[ "$dev_mode" == "true" ]]; then
  rm -rf "$cache_dir"
  log "dev mode: symlinking cache → $plugin_dir"
  ln -s "$plugin_dir" "$cache_dir"
elif [[ -d "$cache_dir" && ! -L "$cache_dir" ]]; then
  # Refresh IN PLACE: a running runner calls scripts out of this dir (and
  # holds files open on NFS), so never rm it — a same-version reinstall once
  # deleted execute-code.sh under a live sweep.
  log "refreshing cache at $cache_dir (copy over, in place)"
  cp -a "$plugin_dir/." "$cache_dir/"
else
  rm -rf "$cache_dir"
  log "populating cache at $cache_dir (copy)"
  cp -a "$plugin_dir" "$cache_dir"
fi

# --- Update known_marketplaces.json ----------------------------------------
# In dev mode, set autoUpdate=false so a marketplace sync doesn't clobber the
# symlink with a fresh copy.
python3 - "$PLUGINS_DIR/known_marketplaces.json" "$marketplace_name" \
         "$source_kind" "$marketplace_dir" "$REPO_DEFAULT" "$dev_mode" <<'PY'
import json, pathlib, sys, datetime
path, name, kind, install_loc, repo, dev = sys.argv[1:]
data = {}
p = pathlib.Path(path)
if p.exists():
    try: data = json.loads(p.read_text())
    except Exception: data = {}
now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
source = {"source": "github", "repo": repo} if kind == "git" \
         else {"source": "directory", "path": install_loc}
entry = {"source": source, "installLocation": install_loc, "lastUpdated": now}
if dev == "true":
    entry["autoUpdate"] = False
data[name] = entry
p.write_text(json.dumps(data, indent=2) + "\n")
PY

# --- Update installed_plugins.json -----------------------------------------
python3 - "$PLUGINS_DIR/installed_plugins.json" "$PLUGIN_NAME" \
         "$marketplace_name" "$cache_dir" "$plugin_version" <<'PY'
import json, pathlib, sys, datetime
path, pname, mname, cache, version = sys.argv[1:]
p = pathlib.Path(path)
data = {"version": 2, "plugins": {}}
if p.exists():
    try: data = json.loads(p.read_text())
    except Exception: pass
data.setdefault("version", 2)
data.setdefault("plugins", {})
# Remove any prior install of this plugin under any marketplace.
for k in [k for k in data["plugins"] if k.startswith(f"{pname}@")]:
    data["plugins"].pop(k)
now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
data["plugins"][f"{pname}@{mname}"] = [{
    "scope": "user", "installPath": cache, "version": version,
    "installedAt": now, "lastUpdated": now,
}]
p.write_text(json.dumps(data, indent=2) + "\n")
PY

log "installed $PLUGIN_NAME v$plugin_version from $source_kind (marketplace: $marketplace_name)"
log "next: open Claude Code (or run /reload-plugins in an existing session)"
