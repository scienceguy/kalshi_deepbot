# Paper-run persistence (Termux::Boot)

`30-kalshi-paper` restarts the dry paper loop after a device reboot.
It is DRY ONLY: no `--live`, and it must never gain one.

## Install (once per device)
```bash
mkdir -p ~/.termux/boot
cp scripts/boot/30-kalshi-paper ~/.termux/boot/
chmod +x ~/.termux/boot/30-kalshi-paper
termux-wake-lock          # held for the session; survives suspend better
```

## Reverse
```bash
rm ~/.termux/boot/30-kalshi-paper
```

Requires the Termux:Boot app. A fresh clone does **not** auto-persist:
this directory is the source of truth, `~/.termux/boot` is the install target.
