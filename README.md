# VSH101 Real-Time ECG Monitor (PC Bluetooth)

A Python application for real-time ECG acquisition and visualization from the **VitalSigns Technology VSH101 1-Lead Holter** device using PC Bluetooth (BLE).

---

## Features

- **BLE direct connection** — no dongle required, uses the PC's built-in Bluetooth adapter
- **Auto-scan & device selection** — automatically discovers nearby VSH101 devices on startup
- **Real-time ECG waveform** — 10-second scrolling display at 500 Hz
- **Heart Rate trend chart** — live HR history plot
- **Vital signs panel** — Heart Rate, Temperature, Battery, RR Interval, packet rate
- **Start / Stop / Clear buttons** — full control of measurement from the GUI
- **ACK-verified command flow** — each BLE command is confirmed before proceeding
- **Demo mode** — simulate ECG without hardware for testing

---

## Hardware Requirements

| Item | Details |
|------|---------|
| **VSH101** | VitalSigns Technology 1-Lead Holter ECG device |
| **PC Bluetooth** | BLE 4.0+ adapter (Intel Wireless Bluetooth or equivalent) |
| **OS** | Windows 10/11, macOS, Linux |

---

## BLE Protocol

The VSH101 uses the **Nordic UART Service (NUS)**:

| Role | UUID |
|------|------|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| Write (PC → device) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| Notify (device → PC) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |

---

## Installation

### 1. Prerequisites

- Python 3.10 or newer

### 2. Install dependencies

```bash
pip install bleak matplotlib numpy
```

| Package | Purpose |
|---------|---------|
| `bleak` | Cross-platform BLE communication |
| `matplotlib` | Real-time ECG waveform plotting |
| `numpy` | ECG signal processing |

### 3. Download the script

```bash
git clone https://github.com/<your-repo>/VSH101-ECG.git
cd VSH101-ECG
```

Or download `VSH101_BLE.py` directly.

---

## VSH101 Device Setup

1. **Power on** — press and hold the button for 5 seconds until the **green LED blinks**
2. **BLE advertising** — the device starts advertising automatically after power-on
3. **Electrode placement** — attach the ECG electrodes to the device following the VSH101 manual
4. **Range** — keep the device within **1 metre** of the PC during initial connection

---

## Usage

### Auto-scan (recommended)

```bash
python VSH101_BLE.py
```

The program will:
1. Scan for nearby VSH101 devices (6 seconds)
2. If one device is found → connect automatically
3. If multiple devices are found → display a numbered list and prompt for selection

```
[12:34:56.001] [SCAN] Scanning 6s for BLE devices (PC Bluetooth)...
[12:34:57.123]   VSH101_JL_DBG               CC:CC:CC:90:BA:2B  RSSI:  -55  <-- VSH101
[12:34:57.124]   VSH101_JL_REL               DD:DD:DD:11:22:33  RSSI:  -72  <-- VSH101

Multiple devices found, please choose:
  [0] VSH101_JL_DBG           (CC:CC:CC:90:BA:2B)
  [1] VSH101_JL_REL           (DD:DD:DD:11:22:33)
Enter index to connect: 0
```

### Direct connect via MAC address

```bash
python VSH101_BLE.py --mac CC:CC:CC:90:BA:2B
```

### Scan only (list devices without connecting)

```bash
python VSH101_BLE.py --scan-only
```

### Demo mode (no hardware needed)

```bash
python VSH101_BLE.py --demo
```

Generates a synthetic PQRST waveform for UI testing.

### All options

```
usage: VSH101_BLE.py [-h] [--mac MAC] [--type {0,1}]
                     [--scan-only] [--scan-timeout SCAN_TIMEOUT] [--demo]

optional arguments:
  --mac MAC                  VSH101 BLE MAC address (omit to trigger auto-scan)
  --type {0,1}               VSC Mode: 0=2ch/968B  1=1ch/568B (default: 1)
  --scan-only                Scan for VSH101 devices and exit
  --scan-timeout SECONDS     Scan duration in seconds (default: 6)
  --demo                     Simulate ECG without hardware
```

---

## GUI Controls

| Button | Action |
|--------|--------|
| **START** | Begin ECG measurement — sends VSC Mode commands and starts data streaming |
| **STOP** | Pause measurement — sends VSC Mode Stop to device |
| **CLEAR** | Clear ECG waveform and HR history from display |

---

## Communication Flow

After pressing **START**, the program follows the official VSH101 command sequence:

```
Step 1  Version Get       →  reads firmware version string from device
Step 2  VSC Type Set      →  sets VSC Mode Type 1 (1-channel, 568 B/packet)
Step 3  VSC Mode START    →  device begins buffering ECG samples
Step 4  VSC Mode READ     →  polls ECG data every 200 ms (500 Hz, 100 samples/packet)
Step 5  VSC Mode STOP     →  stops streaming (on STOP button or window close)
```

Each of Steps 1–3 and 5 waits for a BLE ACK notification from the device before proceeding. If an ACK is not received within 3 seconds, the sequence is **aborted** and an error is displayed in the GUI.

### Packet structure (VSC Mode Read response, Type 1)

```
Byte offset   Field
──────────────────────────────────────────────
0             PCode  (0x6A)
1             Group  (0xC2)
2             Ack    (0x41 = 'A' = OK)
3             ChkSum
4–5           Index  (uint16 LE)
6–7           Length (uint16 LE)
8–407         ECG data: 100 × float32 (ch0, filtered)
408–575       INFO data: 42 × int32
              [1]  Temperature × 10  (0.1 °C)
              [2]  Heart Rate (bpm)
              [3]  Lead-off flag
              [7]  Battery SOC (%)
              [9]  RR Interval (ms)
```

---

---

## VSC MODE Commands — Detailed Reference

All packets share the same **8-byte header + 16-byte parameter** structure.  
The checksum covers every byte in the packet: `ChkSum = sum(all_bytes) % 256`.

### General Packet Format

```
Offset  Size  Field       Description
──────────────────────────────────────────────────────────────────
0       1 B   PCode       Command code (identifies the command)
1       1 B   Group       Command group (0xC0 = system, 0xC2 = VSC)
2       1 B   Cmd / Ack   TX: same as PCode  |  RX: 0x41=OK / 0x4E=fail
3       1 B   ChkSum      sum(all bytes in packet) % 256
4       2 B   MOSI_LEN    Bytes sent TO device (Little Endian)
6       2 B   MISO_LEN    Bytes expected FROM device (Little Endian)
8      16 B   Param       Parameter field (command-specific, zero-padded)
24+     N B   CmdData     Optional payload (command-specific)
```

---

### 1. Device Version Get

Reads the firmware version string from VSH101.
Must be sent **before** VSC Mode Type Set.

**TX** (24 bytes)

```
00 C0 00 E0  00 00 20 00  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
^^ header ^^ ^^^^ lens ^^  ^^^^^^^^^^^^^^^^ 16-byte param ^^^^^^^^^^^^^^^^
```

| Field | Value | Notes |
|-------|-------|-------|
| PCode / Cmd | `0x00` | Version Get |
| Group | `0xC0` | System group |
| MOSI_LEN | `0x0000` | No payload sent |
| MISO_LEN | `0x0020` | Expects 32 B payload back |
| ChkSum | `0xE0` | Pre-computed |

**RX ACK** — 40 bytes (8 B header + 32 B version string)

```
Offset  Field
0       PCode  = 0x00
1       Group  = 0xC0
2       Ack    = 0x41  ('A' = success)
3       ChkSum
4-5     Index  = 0x0000
6-7     Length = 0x0020
8-39    Version string (ASCII, null-padded, e.g. "VSH101_43")
```

**Python:**
```python
CMD_VERSION = bytes([0x00, 0xC0, 0x00, 0xE0,
                     0x00, 0x00, 0x20, 0x00]) + bytes(16)
# write with response=True, wait for 40-byte notify
```

**BLE write type:** `response=True` | **ACK length:** 40 B | **Abort on fail:** No (warning only)

---

### 2. VSC Mode Type Set

Selects the VSC data format. Must be sent after Version Get and before VSC START.

**TX** (28 bytes)

```
70 C2 70 ??  04 00 00 00  00 00 00 00 ... 00   [01 00 00 00]
                                               ^^ CmdData ^^
```

| Field | Value | Notes |
|-------|-------|-------|
| PCode / Cmd | `0x70` | VSC Type Set |
| Group | `0xC2` | VSC group |
| MOSI_LEN | `0x0004` | 4 bytes CmdData |
| MISO_LEN | `0x0000` | No payload back |
| CmdData | uint32 LE | `0x00000000` = Type-0, `0x00000001` = Type-1 |

**VSC Type values:**

| Value | Type | ECG channels | Payload size | Total response |
|-------|------|-------------|-------------|----------------|
| `0` | Type-0 | 2 ch (filtered + raw) | 968 B | 976 B |
| `1` | Type-1 | 1 ch (filtered only) | 568 B | **576 B** (default) |

**Python:**
```python
import struct
cmd_data = struct.pack("<I", 1)   # Type-1
# ChkSum must be recomputed; see _vsh_pkt() in source
```

**RX ACK** — 8 bytes (header only, MISO_LEN = 0)

```
70 C2 41 ChkSum  00 00  00 00
```

**BLE write type:** `response=True` | **ACK length:** 8 B | **Abort on fail:** **Yes**

---

### 3. VSC Mode START

Instructs VSH101 to begin buffering ECG samples internally.

**TX** (24 bytes)

```
64 C2 64 8A  00 00 00 00  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

| Field | Value | Notes |
|-------|-------|-------|
| PCode / Cmd | `0x64` | VSC Start |
| Group | `0xC2` | VSC group |
| MOSI_LEN | `0x0000` | No payload |
| MISO_LEN | `0x0000` | No payload back |
| ChkSum | `0x8A` | Pre-computed |

**Python:**
```python
CMD_VSC_START = bytes([0x64, 0xC2, 0x64, 0x8A,
                       0x00, 0x00, 0x00, 0x00]) + bytes(16)
```

**RX ACK** — 8 bytes

```
64 C2 41 ChkSum  00 00  00 00
```

**BLE write type:** `response=True` | **ACK length:** 8 B | **Abort on fail:** **Yes**

---

### 4. VSC Mode READ

Requests one ECG data packet. Sent every **200 ms** in a polling loop.
The device responds via BLE Notify with ECG + vital signs payload.

**TX** (24 bytes)

```
6A C2 6A ??  00 00 38 02  [idx_L idx_H 00 00]  [01]  [00 x11]
             ^^^^^^^^^^^  ^^^^ index LE ^^^^   ^^ch   padding
             MISO=0x0238
```

| Field | Value | Notes |
|-------|-------|-------|
| PCode / Cmd | `0x6A` | VSC Read |
| Group | `0xC2` | VSC group |
| MOSI_LEN | `0x0000` | No CmdData |
| MISO_LEN | `0x0238` (568) | Type-1 / `0x03C8` (968) for Type-0 |
| Param[0:4] | index uint32 LE | Rolling 0–999, retry same on not-ready |
| Param[4] | `0x01` | Channel flag |

**Python:**
```python
import struct
def cmd_vsc_read(index: int, vsc_type: int = 1) -> bytes:
    miso = 968 if vsc_type == 0 else 568
    param = bytearray(16)
    struct.pack_into("<I", param, 0, index & 0xFFFFFFFF)
    param[4] = 0x01
    return _vsh_pkt(0x6A, 0xC2, 0x6A, mosi_len=0, miso_len=miso, param=bytes(param))
```

**RX response** — 576 bytes (Type-1) / 976 bytes (Type-0)

```
Offset   Size   Field
──────────────────────────────────────────────────────────────────
0        1 B    PCode  = 0x6A
1        1 B    Group  = 0xC2
2        1 B    Ack    = 0x41 (ready) / 0x4E (not ready -> retry)
3        1 B    ChkSum
4-5      2 B    Index  (echoed back; 0x8000 = not ready)
6-7      2 B    Length = 0x0238 (568 B)
8-407   400 B   ECG data: 100 x float32, ch0 filtered (unit: mV)
408-575 168 B   INFO data: 42 x int32 (Little Endian)
```

**INFO block** (byte 408, 42 x int32):

| Index | Field | Unit / Notes |
|-------|-------|-------------|
| `[0]` | Total seconds | Elapsed recording time |
| `[1]` | Temperature | x10, 0.1 C (e.g. 365 = 36.5 C) |
| `[2]` | Heart Rate | bpm (valid: 20-299) |
| `[3]` | Lead-off flag | 0 = OK, 1 = lead disconnected |
| `[4]` | G-sensor X | Raw accelerometer |
| `[5]` | G-sensor Y | Raw accelerometer |
| `[6]` | G-sensor Z | Raw accelerometer |
| `[7]` | Battery SOC | % (0-100) |
| `[8]` | Battery total seconds | — |
| `[9]` | RR Interval | ms |
| `[10]` | SDNN | HRV metric (ms) |
| `[11]` | NN50 | HRV metric |
| `[12]` | RMSSD | HRV metric (ms) |
| `[13]` | LF | HRV frequency domain |
| `[14]` | HF | HRV frequency domain |
| `[15]` | VLF | HRV frequency domain |
| `[16]` | TP | Total HRV power |

**Not-ready handling:**
```
- Ack = 0x4E  OR  Index >= 0x8000  ->  data not ready
- Do NOT advance index; retry same index on next loop iteration
- Each successful packet = 200 ms of ECG (100 samples at 500 Hz)
```

**BLE write type:** `response=True` | **ACK length:** 576 B (Type-1) | **Abort on fail:** No (retry)

---

### 5. VSC Mode STOP

Stops ECG streaming. Sent on STOP button press or window close.

**TX** (24 bytes)

```
65 C2 65 8C  00 00 00 00  00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

| Field | Value | Notes |
|-------|-------|-------|
| PCode / Cmd | `0x65` | VSC Stop |
| Group | `0xC2` | VSC group |
| MOSI_LEN | `0x0000` | No payload |
| MISO_LEN | `0x0000` | No payload back |
| ChkSum | `0x8C` | Pre-computed |

**Python:**
```python
CMD_VSC_STOP = bytes([0x65, 0xC2, 0x65, 0x8C,
                      0x00, 0x00, 0x00, 0x00]) + bytes(16)
```

**RX ACK** — 8 bytes

```
65 C2 41 ChkSum  00 00  00 00
```

**BLE write type:** `response=True` | **ACK length:** 8 B | **Abort on fail:** No (warning only)

---

### VSC Command Quick Reference

| Step | Command | PCode | Group | MOSI | MISO | ACK len | Abort on fail |
|------|---------|-------|-------|------|------|---------|---------------|
| 1 | Version Get | `0x00` | `0xC0` | 0 B | 32 B | **40 B** | No |
| 2 | VSC Type Set | `0x70` | `0xC2` | 4 B | 0 B | **8 B** | **Yes** |
| 3 | VSC Mode START | `0x64` | `0xC2` | 0 B | 0 B | **8 B** | **Yes** |
| 4 | VSC Mode READ | `0x6A` | `0xC2` | 0 B | 568/968 B | **576/976 B** | No (retry) |
| 5 | VSC Mode STOP | `0x65` | `0xC2` | 0 B | 0 B | **8 B** | No |

> All commands use `write_gatt_char(..., response=True)` (ATT Write Request).
> The device sends its ACK/response via BLE Notify on `6e400003-...`.


## Console Output

A typical successful startup looks like:

```
[12:34:56.001] [SCAN] Scanning 6s for BLE devices...
[12:34:57.123]   VSH101_JL_DBG    CC:CC:CC:90:BA:2B  RSSI: -55  <-- VSH101
[12:34:57.124] [MAIN] Automatically selected: VSH101_JL_DBG (CC:CC:CC:90:BA:2B)
[12:34:57.200] ============================================================
[12:34:57.201]   VSH101 ECG Real-Time Monitor  (PC Bluetooth)
[12:34:57.202]   MAC      : CC:CC:CC:90:BA:2B
[12:34:57.203]   VSC Type : 1
[12:34:58.500] [BLE] Connected!
[12:34:58.600] [BLE] Notifications enabled

--- press START in the GUI ---

[12:35:01.000] [TX]  Version Get       (24B): 00c000e0...  sent OK
[12:35:01.050] [ACK] Version Get       OK  PCode=00 Group=C0 Ack=41 ChkSum=44  (40B): ...
[12:35:01.051] [VERSION] | VSH101 Firmware : VSH101_43  |

[12:35:01.100] [TX]  VSC Type Set (1)  (28B): 70c270ab...  sent OK
[12:35:01.150] [ACK] VSC Type Set (1)  OK

[12:35:01.200] [TX]  VSC Mode START    (24B): 64c2648a...  sent OK
[12:35:01.250] [ACK] VSC Mode START    OK

[12:35:01.300] [VSC] Streaming ECG  (press STOP to stop)...
[12:35:01.300] [TX]  VSC Mode READ  index=0  (24B): 6ac26a...  sent OK
[12:35:01.500] [ACK] VSC Mode READ  index=0  OK
```

---

## Troubleshooting

### No devices found during scan

- Ensure VSH101 is powered on (green LED must be blinking)
- Ensure the PC Bluetooth adapter is enabled
- Move the device closer to the PC (within 1 m)
- Try increasing scan time: `--scan-timeout 15`

### BLE connection fails

- Another application may already be connected — close it first
- Try power-cycling the VSH101 (hold button until LED turns off, then power on again)

### ACK timeout after START

- The device may have gone to sleep — power cycle and reconnect
- Check that electrodes are attached before starting measurement

### `ModuleNotFoundError: No module named 'bleak'`

```bash
pip install bleak matplotlib numpy
```

### `matplotlib` window does not appear (Linux)

Install the Tk backend:
```bash
sudo apt-get install python3-tk
```

### Windows: `Access Denied` on BLE write

Run the terminal as Administrator, or ensure no other app (e.g. the VSH101 mobile app) is connected to the device simultaneously.

---

## Project Structure

```
VSH101-ECG/
├── VSH101_BLE.py       # Main application
└── README.md           # This file
```

---

## References

- [VSH101 Command Table](https://www.vsigntek.com/manual_vsh101_command_table/)
- [VS-ECG SDK Documentation](https://www.vsigntek.com/VS_ECG_SDK/)
- [VitalSigns Technology](https://www.vsigntek.com/)
- [bleak — BLE library for Python](https://github.com/hbldh/bleak)

---

## License

MIT License — see `LICENSE` for details.