#!/usr/bin/env bash
# Sets up localcoder: detects this machine's hardware, writes a config.json
# tuned for it (never overwrites an existing one), and installs a
# `localcoder` launcher on PATH. On Linux + systemd, also installs the
# Ollama systemd --user service (symlinked to this repo's tuning script,
# not copied). On macOS (or Linux without systemd) the launcher is still
# installed; Ollama.app / a manual scripts/ollama-serve-tuned.sh is used
# instead of a second server. See
# docs/superpowers/specs/2026-09-06-macos-support-design.md.
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

# --- 4. Ollama service (Linux + systemd only) --------------------------
if command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$LOCAL_BIN" "$SYSTEMD_USER_DIR"
    chmod +x "$REPO_ROOT/scripts/ollama-serve-tuned.sh"
    ln -sf "$REPO_ROOT/scripts/ollama-serve-tuned.sh" "$LOCAL_BIN/ollama-serve-tuned"
    echo "symlinked $LOCAL_BIN/ollama-serve-tuned -> repo's scripts/ollama-serve-tuned.sh"
    cp "$REPO_ROOT/scripts/ollama-tuned.service" "$SYSTEMD_USER_DIR/ollama-tuned.service"
    systemctl --user daemon-reload
    systemctl --user enable --now ollama-tuned.service
    echo "ollama-tuned.service enabled and started"
else
    mkdir -p "$LOCAL_BIN"
    chmod +x "$REPO_ROOT/scripts/ollama-serve-tuned.sh"
    echo "no systemd -- skipping ollama-tuned.service (expected on macOS)."
    echo "If Ollama.app is already running, use that. Otherwise start:"
    echo "  $REPO_ROOT/scripts/ollama-serve-tuned.sh"
    echo "in its own terminal (do not background it -- see README Troubleshooting)."
fi

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
       echo "On macOS zsh (default), that line belongs in ~/.zprofile."
       ;;
esac

if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11434/api/version', timeout=2)" 2>/dev/null; then
    echo "Ollama is already answering on 127.0.0.1:11434 -- using that (do not start a second server)."
else
    echo "Ollama is not answering on 127.0.0.1:11434 yet. Open Ollama.app or run scripts/ollama-serve-tuned.sh, then: ollama pull qwen2.5-coder:7b"
fi

echo ""
echo "Done. Run 'localcoder' from inside a project directory to start."
echo "First real turn will be slow on CPU-only hardware (cold model load + prefill) -- see README's Troubleshooting section."
