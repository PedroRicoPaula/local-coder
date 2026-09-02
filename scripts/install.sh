#!/usr/bin/env bash
# Sets up localcoder on a Linux machine: detects this machine's hardware,
# writes a config.json tuned for it, installs the Ollama systemd --user
# service (symlinked to this repo's tuning script, not copied -- so future
# repo changes take effect without re-running this), and installs a
# `localcoder` launcher on PATH. Safe to re-run: never overwrites a
# config.json that already exists, and the systemd unit / launcher are
# idempotent (same content every time).
#
# Linux only, by design -- see docs/BACKLOG.md for why macOS isn't covered.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_BIN="$HOME/.local/bin"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo "localcoder install -- repo: $REPO_ROOT"

# --- 1. Prerequisites -------------------------------------------------
command -v python3 >/dev/null 2>&1 || { echo "python3 not found -- install it first."; exit 1; }
if ! command -v ollama >/dev/null 2>&1; then
    echo "ollama not found on PATH -- install it first: https://ollama.com/download"
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found -- this install script only automates the systemd --user path."
    echo "You can still run localcoder manually: start 'ollama serve' yourself"
    echo "(or run scripts/ollama-serve-tuned.sh directly), then use main.py."
    echo "See scripts/ollama-serve-tuned.sh's comments for the env vars it sets and why."
    exit 1
fi

# --- 2. Detect hardware, decide a tier ---------------------------------
echo "Detecting hardware..."
HW_JSON="$(python3 "$REPO_ROOT/scripts/detect_hardware.py")"
echo "$HW_JSON"

PHYSICAL_CORES="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['physical_cores'])" "$HW_JSON")"
TIER="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['tier'])" "$HW_JSON")"

case "$TIER" in
    gpu)
        # Ollama already uses the GPU automatically when a driver is
        # present -- CPU-specific tuning (num_thread/num_batch, the q8_0 KV
        # cache memory trade-off) isn't the relevant lever here, so this
        # tier deliberately leaves them unset and lets Ollama's own
        # defaults apply. quality profile: this tier has headroom for it.
        DEFAULT_PROFILE="quality"
        NUM_THREAD="null"
        NUM_BATCH="null"
        ;;
    cpu-strong)
        DEFAULT_PROFILE="quality"
        NUM_THREAD="$PHYSICAL_CORES"
        NUM_BATCH="2048"
        ;;
    *)  # cpu-weak
        DEFAULT_PROFILE="fast"
        NUM_THREAD="$PHYSICAL_CORES"
        NUM_BATCH="2048"
        ;;
esac
echo "Tier: $TIER -> default_profile=$DEFAULT_PROFILE, num_thread=$NUM_THREAD, num_batch=$NUM_BATCH"

# --- 3. Write config.json (never clobber an existing one) -------------
CONFIG_FILE="$REPO_ROOT/config.json"
if [ -f "$CONFIG_FILE" ]; then
    echo "config.json already exists -- leaving it untouched."
    echo "Delete it and re-run this script to regenerate it from detected hardware."
else
    python3 - "$CONFIG_FILE" "$DEFAULT_PROFILE" "$NUM_THREAD" "$NUM_BATCH" <<'PYEOF'
import json
import sys

path, default_profile, num_thread, num_batch = sys.argv[1:5]
cfg = {
    "ollama_host": "http://127.0.0.1:11434",
    "request_timeout_s": 600,
    "num_ctx": 8192,
    "default_profile": default_profile,
}
if num_thread != "null":
    cfg["num_thread"] = int(num_thread)
if num_batch != "null":
    cfg["num_batch"] = int(num_batch)
# "model" is deliberately NOT written here -- leaving it unset means
# config.py's DEFAULTS/model_profiles decide it via default_profile,
# instead of pinning a value install.sh would otherwise have to keep in
# sync with config.py's model_profiles by hand.
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"wrote {path}")
PYEOF
fi

# --- 4. Ollama systemd --user service, symlinked to the repo's script --
mkdir -p "$LOCAL_BIN" "$SYSTEMD_USER_DIR"
chmod +x "$REPO_ROOT/scripts/ollama-serve-tuned.sh"
ln -sf "$REPO_ROOT/scripts/ollama-serve-tuned.sh" "$LOCAL_BIN/ollama-serve-tuned"
echo "symlinked $LOCAL_BIN/ollama-serve-tuned -> repo's scripts/ollama-serve-tuned.sh"

cp "$REPO_ROOT/scripts/ollama-tuned.service" "$SYSTEMD_USER_DIR/ollama-tuned.service"
systemctl --user daemon-reload
systemctl --user enable --now ollama-tuned.service
echo "ollama-tuned.service enabled and started"

# --- 5. `localcoder` launcher on PATH ----------------------------------
cat > "$LOCAL_BIN/localcoder" <<EOF
#!/usr/bin/env bash
exec python3 "$REPO_ROOT/main.py" "\$@"
EOF
chmod +x "$LOCAL_BIN/localcoder"
echo "installed $LOCAL_BIN/localcoder"

case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *) echo "NOTE: $LOCAL_BIN is not on your PATH -- add it in your shell's rc file:"
       echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
       ;;
esac

echo ""
echo "Done. Run 'localcoder' from inside a project directory to start."
echo "First real turn will be slow on CPU-only hardware (cold model load + prefill) -- see README's Troubleshooting section."
