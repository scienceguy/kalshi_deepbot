# Handoff: XGBoost and LightGBM Installation

## Status
- **Task**: Install and enable xgboost and lightgbm
- **Current State**: WORKING via proot-distro debian container
- **Completed**: 2026-11-05 ~20:10 UTC
- **Environment**: Termux on Android (aarch64)

## Working Solution

### proot-distro debian container
xgboost 3.4.1, lightgbm 4.7.0, scipy 1.18.1, narwhals 2.28.0 — all working.

**Wrapper script**: `./ml_python` in repo root
```bash
./ml_python -c "import xgboost; import lightgbm; print('OK')"
./ml_python my_script.py
./ml_python -m my_module
```

**Direct access**:
```bash
proot-distro login debian -- /usr/bin/python3 -c "import xgboost; import lightgbm"
```

### Why proot?
- Native Termux Python targets `android-24-arm64_v8a` (Bionic libc)
- Pre-built xgboost/lightgbm wheels are `manylinux_2_28_aarch64` (glibc)
- glibc binaries don't load on Android's Bionic libc
- Native xgboost built from source but **scipy has no Android wheel and won't build from source** → native xgboost can't import scipy.sparse
- proot-distro provides full debian with glibc where pre-built wheels work

## Native Build Status
- xgboost 3.4.1 and lightgbm 4.7.0 built natively (android_24_arm64_v8a wheels)
- But scipy and narwhals have no Android pre-built wheels and scipy fails to build from source
- Native xgboost fails: `ModuleNotFoundError: No module named 'scipy'`
- **Use proot for all ML work**

## Disk Space
- Current: 13G available on /data/user/0

## Next Steps
1. Use `./ml_python` wrapper for any ML work
2. If native scipy is needed, try building with `--no-build-isolation` or find a wheel
3. Otherwise, proot is the working path