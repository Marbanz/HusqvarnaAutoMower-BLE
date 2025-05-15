# HusqvarnaAutoMower-BLE

This is an unofficial, reverse-engineered Husqvarna Automower Connect BLE library. It allows you to connect to and control an Automower without requiring any accounts, cloud services, or network connections.

This library was originally developed by **@alistair23**. This fork enhances integration with Home Assistant via the HACS custom integration [HACS-husqvarna_automower_ble](https://github.com/Marbanz/HACS-husqvarna_automower_ble).

Details on the original reverse-engineering process are available here:  
[Reverse Engineering Automower BLE](https://www.alistair23.me/2024/01/06/reverse-engineering-automower-ble)

---

## Installation

This library can be installed by:

```bash
pip install husqvarna-automower-ble
```

> **Note:** This library is under active development and the PyPI version may not be up to date.

---

## Testing Connections

You can test querying data from your Automower using the following commands:

### Query Mower Data
```bash
python -m husqvarna_automower_ble.mower --address D8:B6:73:40:07:37
```

### Send Commands to the Mower
```bash
python -m husqvarna_automower_ble.mower --address D8:B6:73:40:07:37 --command park
```

### Available Commands:
- `park`: Park the mower.
- `pause`: Pause the mower.
- `override`: Override the current schedule (runs for 3 hours by default).
- `resume`: Resume the mower's operation.

---

## Contributing

If you'd like to contribute to this project, feel free to submit issues or pull requests. Testing on different Automower models is especially appreciated!

---

## Disclaimer

This is an unofficial library and is not affiliated with or endorsed by Husqvarna. Use this library at your own risk.
