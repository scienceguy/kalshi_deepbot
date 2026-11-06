# Handoff — Kalshi SuperBot Environment Setup

**Date:** 2026-11-05
**Status:** Complete

---

## 1. XGBoost & LightGBM (ML packages)

**Working solution:** proot-distro debian container

xgboost 3.4.1, lightgbm 4.7.0, scipy 1.18.1, narwhals 2.28.0 — all installed and verified.

**Wrapper script:** `./ml_python` in repo root
```bash
./ml_python -c "import xgboost; import lightgbm; print('OK')"
./ml_python my_script.py
./ml_python -m my_module
```

**Why proot:** Native Termux Python targets `android-24-arm64_v8a` (Bionic libc). Pre-built wheels are `manylinux_2_28_aarch64` (glibc). Native xgboost built from source but scipy has no Android wheel and won't build — native xgboost can't import `scipy.sparse`.

**Native build cleanup:** Removed native xgboost/lightgbm copies (glibc .so won't load on Android).

---

## 2. claude-mem (persistent memory for OpenCode)

**Status:** Active and working

- **Plugin:** `~/.config/opencode/plugins/claude-mem.js` (auto-loaded by OpenCode)
- **MCP server:** wired in `opencode.json` → `claude-mem` local MCP
- **Worker:** `http://127.0.0.1:37718` (healthy)
- **Observer provider:** OpenRouter free `meta-llama/llama-3.3-70b-instruct:free`
- **Settings:** `~/.claude-mem/settings.json`
- **Memory DB:** `~/.claude-mem/claude-mem.db`

**Tree-sitter patch:** `RWe()` function in `worker-service.cjs` patched to return `"disabled"` instead of attempting download. Original at `.bak`. File Read Gate also disabled via `CLAUDE_MEM_FILE_READ_GATE_ENABLED: false`.

**What works:** Memory capture, session summaries, search, timeline, get_observations, MCP tools.

---

## 3. OpenCode CLI — Kilo Auto Free subagent delegation

**Config file:** `/data/data/com.termux/files/home/.config/opencode/opencode.json`

**Kilo provider:** OpenAI-compatible → `http://127.0.0.1:18900/v1` with API key `ofm-DCvYp4cupz_aLGQpB8VkQYKEiGv0--00`

**Agent model assignments:**

| Agent | Mode | Model | Rotation |
|-------|------|-------|----------|
| `build` | primary | `kilo/kilo-auto/free` | None |
| `plan` | primary | `kilo/kilo-auto/free` | None |
| `general` | subagent | `kilo/combo:recommended-free` | Auto on 429 |
| `explore` | subagent | `kilo/combo:recommended-free` | Auto on 429 |

**Top-level model:** `kilo/kilo-auto-free`

**Gateway models available:** kilo-auto/free, combo:recommended-free, deepseek-v4.1-flash-free, glm-5.3-flash-free, nemotron-3.5-lightning-free, mimo-v2.6-flash-free, flint-40b-free, beam-free, big-pickle, and more.

**Provider options:** `timeout: 120000`, `chunkTimeout: 60000`

**All providers preserved:** nvidia, opencode-free, openrouter-free, groq-free, google-ai-studio, kilo

**Rotation script:** `/data/data/com.termux/files/home/subagent_rotation.sh` (updated)

---

## Disk Space

- Current: ~13G available on `/data/user/0`
- Freed ~4GB by removing old bot directories, caches, and opencode DB

## Key Files

| File | Purpose |
|------|---------|
| `handoff.md` | This file |
| `ml_python` | proot wrapper for ML packages |
| `/data/data/com.termux/files/home/.config/opencode/opencode.json` | OpenCode config |
| `/data/data/com.termux/files/home/.claude-mem/settings.json` | claude-mem settings |
| `/data/data/com.termux/files/home/.claude-mem/claude-mem.db` | Memory database |
| `worker-service.cjs.bak` | Original tree-sitter patch backup |

## Verification Commands

```bash
# ML packages
./ml_python -c "import xgboost; import lightgbm; print('OK')"

# claude-mem
curl -s http://127.0.0.1:37718/health
npx claude-mem doctor

# Kilo gateway
curl -s http://127.0.0.1:18900/v1/models -H "Authorization: Bearer ofm-DCvYp4cupz_aLGQpB8VkQYKEiGv0--00" | python3 -m json.tool

# OpenCode config
python3 -c "import json; c=json.load(open('/data/data/com.termux/files/home/.config/opencode/opencode.json')); print(c['model'], list(c['providers'].keys()))"