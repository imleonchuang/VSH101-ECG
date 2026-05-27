"""
VSH101 ECG Real-Time Monitor  (PC Bluetooth / Intel Wireless BLE)
==================================================================
Communication path:  PC Intel(R) Wireless Bluetooth  →  BLE  →  VSH101

Based on official VitalSigns Technology documentation:
  https://www.vsigntek.com/manual_vsh101_command_table/

BLE Profile (Nordic UART Service):
  Service  : 6e400001-b5a3-f393-e0a9-e50e24dcca9e
  Write TX : 6e400002-b5a3-f393-e0a9-e50e24dcca9e  (PC → VSH101)
  Notify RX: 6e400003-b5a3-f393-e0a9-e50e24dcca9e  (VSH101 → PC)

Install:
    pip install bleak matplotlib numpy

Usage:
    python vsh101_ble.py                           # auto-scan and choose/connect
    python vsh101_ble.py --mac CC:CC:CC:90:BA:2B   # direct connect via specify MAC
    python vsh101_ble.py --scan-only               # scan and list VSH101 devices
    python vsh101_ble.py --demo                    # simulate without hardware
"""

import asyncio
import struct
import threading
import time
import argparse
from collections import deque

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.widgets import Button
from bleak import BleakClient, BleakScanner


# ─────────────────────────────────────────────────────────────────
# Configuration  ← edit here if needed
# ─────────────────────────────────────────────────────────────────
DEFAULT_MAC   = None   # Set to None to trigger auto-scan by default

# VSC Mode type:  0 = Type-0 (968 B, 2-ch ECG)
#                 1 = Type-1 (568 B, 1-ch ECG)  ← default per official sample
VSC_MODE_TYPE = 1

# BLE UUIDs  — VSH101 uses Nordic UART Service
BLE_WRITE_UUID  = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # PC  → device
BLE_NOTIFY_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # device → PC

# Display / timing
DISPLAY_SECONDS  = 10
SAMPLE_RATE      = 500.0    # Hz
SAMPLES_PER_PKT  = 100      # samples per channel per packet (200 ms)
PACKET_INTERVAL  = 0.200    # seconds
DISPLAY_SAMPLES  = int(SAMPLE_RATE * DISPLAY_SECONDS)


# ─────────────────────────────────────────────────────────────────
# Time-stamped Log Helper (HH:MM:SS.mmm format)
# ─────────────────────────────────────────────────────────────────
def _log_msg(msg: str):
    """Prints a message prefixed with current time as [HH:MM:SS.mmm]."""
    t = time.time()
    milli = int((t - int(t)) * 1000)
    t_str = time.strftime("%H:%M:%S", time.localtime(t))
    print(f"[{t_str}.{milli:03d}] {msg}")


# ─────────────────────────────────────────────────────────────────
# VSH101 BLE packet builder  (direct — no VSM004 wrapper)
# ─────────────────────────────────────────────────────────────────
_PARAM_PAD = bytes(16)

def _chk(data: bytes) -> int:
    return sum(data) % 256

def _vsh_pkt(pcode, group, cmd, mosi_len, miso_len,
             param: bytes = None, cmd_data: bytes = b"") -> bytes:
    """Build a VSH101 BLE command packet with correct checksum."""
    if param is None:
        param = _PARAM_PAD
    # Build header without checksum first to compute it
    hdr = bytes([pcode, group, cmd, 0x00,
                 mosi_len & 0xFF, (mosi_len >> 8) & 0xFF,
                 miso_len & 0xFF, (miso_len >> 8) & 0xFF])
    raw = hdr + param + cmd_data
    chk = _chk(raw)
    return bytes([pcode, group, cmd, chk,
                  mosi_len & 0xFF, (mosi_len >> 8) & 0xFF,
                  miso_len & 0xFF, (miso_len >> 8) & 0xFF]) + param + cmd_data


# ── Pre-built command packets ─────────────────────────────────────
CMD_VERSION   = _vsh_pkt(0x00, 0xC0, 0x00, mosi_len=0, miso_len=0x0020)
CMD_VSC_START = _vsh_pkt(0x64, 0xC2, 0x64, mosi_len=0, miso_len=0)
CMD_VSC_STOP  = _vsh_pkt(0x65, 0xC2, 0x65, mosi_len=0, miso_len=0)

def cmd_vsc_type_set(vsc_type: int) -> bytes:
    """VSC Mode Type Set — CmdData = uint32 LE (0=Type-0, 1=Type-1)."""
    return _vsh_pkt(0x70, 0xC2, 0x70, mosi_len=4, miso_len=0,
                    cmd_data=struct.pack("<I", vsc_type))

def cmd_vsc_read(index: int, vsc_type: int = 1) -> bytes:
    """
    VSC Mode Read packet.
    param[0:4] = index (LE uint32), param[4] = 0x01 (channel flag).
    MISO = 968 (Type-0) or 568 (Type-1).
    """
    miso = 968 if vsc_type == 0 else 568
    param = bytearray(16)
    struct.pack_into("<I", param, 0, index & 0xFFFFFFFF)
    param[4] = 0x01
    return _vsh_pkt(0x6A, 0xC2, 0x6A, mosi_len=0, miso_len=miso,
                    param=bytes(param))


# ─────────────────────────────────────────────────────────────────
# VSH101 response parser
# ─────────────────────────────────────────────────────────────────
ECG_BYTES    = {0: 800, 1: 400}   # bytes per response
INFO_BYTES   = 168                 # 42 × int32
RESP_HDR_LEN = 8
RESP_TOTAL   = {0: RESP_HDR_LEN + 968,    # 976
                1: RESP_HDR_LEN + 568}    # 576
IDX_INVALID  = 0x8000              # device returns this when data not ready

def parse_vsc(raw: bytes, vsc_type: int = 1) -> dict:
    """
    Parse a VSC Mode Read BLE response.
    """
    result = {"valid": False, "not_ready": False,
              "ecg": [], "hr": 0, "temp": 0.0,
              "battery": 0, "rr_ms": 0, "lead_off": 0, "index": -1}

    if len(raw) < RESP_TOTAL[vsc_type]:
        return result

    if raw[0] != 0x6A or raw[1] != 0xC2:
        return result

    if raw[2] != 0x41:          # Ack byte: 'A'=ready, 'N'=not ready
        result["not_ready"] = True
        return result

    idx = struct.unpack_from("<H", raw, 4)[0]
    result["index"] = idx
    if idx >= IDX_INVALID:
        result["not_ready"] = True
        return result

    payload  = raw[RESP_HDR_LEN:]
    ecg_n    = ECG_BYTES[vsc_type] // 4
    ecg_all  = struct.unpack_from(f"<{ecg_n}f", payload, 0)
    result["ecg"] = list(ecg_all[:SAMPLES_PER_PKT])

    info_off = ECG_BYTES[vsc_type]
    if len(payload) >= info_off + INFO_BYTES:
        info = struct.unpack_from(f"<{INFO_BYTES // 4}i", payload, info_off)
        if len(info) >= 10:
            result["temp"]     = info[1] / 10.0
            hr = info[2]
            result["hr"]       = hr if 20 < hr < 300 else 0
            result["lead_off"] = info[3]
            result["battery"]  = info[7]
            rr = info[9]
            result["rr_ms"]    = rr if rr > 0 else 0

    result["valid"] = True
    return result


# ─────────────────────────────────────────────────────────────────
# BLE Manager
# ─────────────────────────────────────────────────────────────────
class BLEManager:
    def __init__(self, mac: str, vsc_type: int = VSC_MODE_TYPE):
        self.mac       = mac
        self.vsc_type  = vsc_type
        self.client: BleakClient = None

        self.connected    = False
        self.measuring    = False
        self._vsc_index   = 0

        self.ecg_buffer   = deque(maxlen=DISPLAY_SAMPLES)
        self.hr_history   = deque(maxlen=120)
        self.packet_count = 0
        self.status = {"hr": "--", "temp": "--", "battery": "--",
                       "rr": "--", "pkt_s": 0.0, "lead_off": 0}

        self._rx_buf     = bytearray()
        self._last_pkt_t = time.time()

        self._pending_len   = None
        self._pending_buf   = bytearray()
        self._pending_event = threading.Event()
        self._pending_data  = None

        self._loop: asyncio.AbstractEventLoop = None

    def _on_notify(self, _sender, data: bytearray):
        if self._pending_len is not None:
            self._pending_buf.extend(data)
            if len(self._pending_buf) >= self._pending_len:
                raw = bytes(self._pending_buf[:self._pending_len])
                self._pending_data = raw
                self._pending_len  = None
                self._pending_event.set()
                if len(raw) >= 2 and raw[0] == 0x6A and raw[1] == 0xC2:
                    self._process(raw)
            return

        self._rx_buf.extend(data)
        expected = RESP_TOTAL[self.vsc_type]

        while len(self._rx_buf) >= expected:
            sync = 0
            while sync < len(self._rx_buf) - 1:
                if self._rx_buf[sync] == 0x6A and self._rx_buf[sync + 1] == 0xC2:
                    break
                sync += 1
            if sync > 0:
                self._rx_buf = self._rx_buf[sync:]

            if len(self._rx_buf) < expected:
                break

            pkt = bytes(self._rx_buf[:expected])
            self._rx_buf = self._rx_buf[expected:]
            self._process(pkt)

    def _process(self, raw: bytes):
        p = parse_vsc(raw, self.vsc_type)
        if not p["valid"] or p["not_ready"]:
            return

        self.ecg_buffer.extend(p["ecg"])

        hr = p["hr"]
        if hr > 0:
            self.hr_history.append(hr)
            self.status["hr"] = f"{hr} bpm"
        if p["temp"] > 0:
            self.status["temp"] = f"{p['temp']:.1f} C"
        if p["battery"] > 0:
            self.status["battery"] = f"{p['battery']}%"
        if p["rr_ms"] > 0:
            self.status["rr"] = f"{p['rr_ms']} ms"
        self.status["lead_off"] = p["lead_off"]

        now = time.time()
        dt  = now - self._last_pkt_t
        if dt > 0:
            self.status["pkt_s"] = round(1.0 / dt, 1)
        self._last_pkt_t = now
        self.packet_count += 1

    async def _write(self, data: bytes):
        await self.client.write_gatt_char(BLE_WRITE_UUID, data, response=False)

    async def _write_req(self, data: bytes):
        await self.client.write_gatt_char(BLE_WRITE_UUID, data, response=True)

    async def _write_req_ack(self, label: str, data: bytes,
                             ack_len: int = 16, timeout: float = 3.0,
                             print_ack: bool = True) -> tuple[bool, bytes | None]:
        self._pending_event.clear()
        self._pending_data = None
        self._pending_buf.clear()
        self._pending_len  = ack_len

        if "VSC RD" not in label: # suppress verbose TX log during high-frequency READ stream
            _log_msg(f"[TX] {label}  ({len(data)}B): {data[:8].hex()} ...")
        
        try:
            await self.client.write_gatt_char(BLE_WRITE_UUID, data, response=True)
        except Exception as e:
            self._pending_len = None
            _log_msg(f"[TX] {label}  FAIL — Write error: {e}")
            return False, None

        if "VSC RD" in label:
            _log_msg(f"[TX] {label}  sent OK")

        ok = await self._loop.run_in_executor(
            None, lambda: self._pending_event.wait(timeout))
        self._pending_len = None

        if not ok or self._pending_data is None:
            _log_msg(f"[ACK] {label}  FAIL — Timeout ({timeout}s), no Notify received")
            return False, None

        ack = self._pending_data

        if len(ack) >= 3 and ack[2] == 0x41:
            if print_ack:
                pcode  = f"{ack[0]:02X}"
                group  = f"{ack[1]:02X}"
                chksum = f"{ack[3]:02X}" if len(ack) > 3 else "??"
                _log_msg(f"[ACK] {label}  OK  "
                         f"PCode={pcode} Group={group} Ack=41 ChkSum={chksum}  "
                         f"({len(ack)}B): {ack.hex()}")
            return True, ack
        else:
            ack_byte = f"{ack[2]:02X}" if len(ack) > 2 else "??"
            _log_msg(f"[ACK] {label}  FAIL — Ack={ack_byte} (expected 41)  "
                     f"({len(ack)}B): {ack.hex()}")
            return False, ack

    async def _read_version(self, timeout: float = 3.0) -> str:
        VERSION_RESP_LEN = 8 + 0x20
        ok, ack = await self._write_req_ack(
            "Version Get", CMD_VERSION,
            ack_len=VERSION_RESP_LEN, timeout=timeout)

        if not ok or ack is None or len(ack) < 8:
            return ""

        payload = ack[8:]
        ver_str = payload.rstrip(b"\x00").decode("ascii", errors="replace").strip()
        return ver_str

    async def _async_connect(self):
        def _on_disconnect(c):
            _log_msg("[BLE] Device disconnected!")
            self.connected = False
            self.measuring = False

        _log_msg(f"[BLE] Connecting to {self.mac} via PC Bluetooth...")
        self.client = BleakClient(
            self.mac, timeout=20.0,
            disconnected_callback=_on_disconnect)
        await self.client.connect()
        self.connected = True
        _log_msg("[BLE] Connected!")

        _log_msg("[BLE] GATT services:")
        for svc in self.client.services:
            for ch in svc.characteristics:
                _log_msg(f"  [{ch.handle:04X}] {ch.uuid}  {list(ch.properties)}")

        await self.client.start_notify(BLE_NOTIFY_UUID, self._on_notify)
        _log_msg(f"[BLE] Notifications enabled  ({BLE_NOTIFY_UUID})")

    async def _async_vsc_loop(self):
        def _abort(reason: str):
            _log_msg(f"[VSC] ABORTED — {reason}")
            self.measuring = False
            self.status["abort"] = reason

        _log_msg("[VERSION] Reading VSH101 firmware version...")
        ver = await self._read_version(timeout=5.0)
        if ver:
            _log_msg(f"[VERSION] +------------------------------+")
            _log_msg(f"[VERSION] |  VSH101 Firmware : {ver:<10s}  |")
            _log_msg(f"[VERSION] +------------------------------+")
        else:
            _log_msg("[VERSION] Could not read version (continuing)")

        ok, _ = await self._write_req_ack(
            f"VSC Type Set ({self.vsc_type})",
            cmd_vsc_type_set(self.vsc_type),
            ack_len=8, timeout=3.0)
        if not ok:
            _abort("VSC Type Set ACK failed — device not responding correctly")
            return

        ok, _ = await self._write_req_ack(
            "VSC Mode START",
            CMD_VSC_START,
            ack_len=8, timeout=3.0, print_ack=False)
        if not ok:
            _abort("VSC Mode START ACK failed — cannot begin measurement")
            return

        READ_ACK_LEN = RESP_TOTAL[self.vsc_type]

        self._vsc_index = 0
        _log_msg("[VSC] Streaming ECG  (press STOP to stop)...")
        _first_read = True
        while self.measuring and self.connected:
            try:
                pkt = cmd_vsc_read(self._vsc_index, self.vsc_type)
                if _first_read:
                    _log_msg(f"[TX] VSC RD Idx={self._vsc_index}  ({len(pkt)}B): {pkt[:8].hex()} ...  sent OK")

                ok, _ = await self._write_req_ack(
                    f"VSC RD Idx={self._vsc_index}",
                    pkt,
                    ack_len=READ_ACK_LEN,
                    timeout=1.0,
                    print_ack=False)

                if _first_read:
                    if ok:
                        _log_msg(f"[ACK] VSC RD Idx={self._vsc_index}  OK")
                    else:
                        _log_msg(f"[ACK] VSC RD Idx={self._vsc_index}  FAIL")
                    _first_read = False

                if ok:
                    self._vsc_index = (self._vsc_index + 1) % 1000
            except Exception as e:
                _log_msg(f"[VSC] Write error: {e}")

        try:
            ok, _ = await self._write_req_ack(
                "VSC Mode STOP",
                CMD_VSC_STOP,
                ack_len=8, timeout=3.0, print_ack=False)
            if not ok:
                _log_msg("[VSC] STOP ACK failed — device may still be streaming")
        except Exception as e:
            _log_msg(f"[VSC] STOP error: {e}")

    async def _async_disconnect(self):
        self.measuring = False
        if self.client and self.connected:
            try:
                ok, _ = await self._write_req_ack(
                    "VSC Mode STOP (disconnect)",
                    CMD_VSC_STOP,
                    ack_len=8, timeout=2.0, print_ack=False)
                if not ok:
                    _log_msg("[VSC] STOP ACK failed during disconnect")
            except Exception:
                pass
            await self.client.disconnect()
        self.connected = False
        _log_msg("[BLE] Disconnected")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start_loop(self):
        self._loop = asyncio.new_event_loop()
        t = threading.Thread(target=self._run_loop, daemon=True, name="BLE-Loop")
        t.start()
        return t

    def connect_sync(self, timeout: float = 25.0) -> bool:
        fut = asyncio.run_coroutine_threadsafe(self._async_connect(), self._loop)
        try:
            fut.result(timeout=timeout)
        except Exception as e:
            _log_msg(f"[BLE] Connection failed: {e}")
            return False
        return self.connected

    def vsc_start(self):
        asyncio.run_coroutine_threadsafe(self._async_vsc_loop(), self._loop)

    def disconnect_sync(self):
        if not self._loop:
            return
        fut = asyncio.run_coroutine_threadsafe(self._async_disconnect(), self._loop)
        try:
            fut.result(timeout=5.0)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────
# ECG Plotter
# ─────────────────────────────────────────────────────────────────
class ECGPlotter:
    def __init__(self, mgr, vsc_thread_fn=None):
        self.mgr           = mgr
        self.vsc_thread_fn = vsc_thread_fn
        self.t_axis        = np.linspace(-DISPLAY_SECONDS, 0, DISPLAY_SAMPLES)

        plt.style.use("dark_background")
        self.fig = plt.figure(figsize=(14, 8), facecolor="#0f0f1a")
        mac_str = getattr(mgr, "mac", "DEMO")
        self.fig.canvas.manager.set_window_title(
            f"VSH101 ECG Monitor  –  PC Bluetooth  –  {mac_str}")

        gs = self.fig.add_gridspec(3, 4, hspace=0.42, wspace=0.28,
                                   left=0.06, right=0.98,
                                   top=0.93, bottom=0.14)

        self.ax_ecg = self.fig.add_subplot(gs[0:2, :])
        self.ax_ecg.set_facecolor("#060610")
        self.ax_ecg.set_title(
            f"ECG Waveform  (ch0 filtered, 500 Hz)  |  {mac_str}",
            color="#00ff88", fontsize=11, fontweight="bold", pad=6)
        self.ax_ecg.set_xlabel("Time (s)", color="#888")
        self.ax_ecg.set_ylabel("Amplitude (mV)", color="#888")
        self.ax_ecg.tick_params(colors="#666")
        self.ax_ecg.set_xlim(-DISPLAY_SECONDS, 0)
        self.ax_ecg.set_ylim(-2.0, 2.0)

        for x in np.arange(-DISPLAY_SECONDS, 0.01, 0.2):
            self.ax_ecg.axvline(x, color="#0e1e0e", linewidth=0.5)
        for x in np.arange(-DISPLAY_SECONDS, 0.01, 1.0):
            self.ax_ecg.axvline(x, color="#1a3a1a", linewidth=0.9)
        for y in np.arange(-2.0, 2.01, 0.5):
            self.ax_ecg.axhline(y, color="#0e1e0e", linewidth=0.5)
        for y in [-1.0, 0.0, 1.0]:
            self.ax_ecg.axhline(y, color="#1a3a1a", linewidth=0.9)

        self.ecg_line, = self.ax_ecg.plot([], [], color="#00ff88",
                                           linewidth=0.7, antialiased=True)
        self.wait_text = self.ax_ecg.text(
            0.5, 0.5, "Press  START  to begin ECG acquisition",
            transform=self.ax_ecg.transAxes,
            ha="center", va="center", fontsize=13,
            color="#334433", style="italic")

        self.ax_hr = self.fig.add_subplot(gs[2, :3])
        self.ax_hr.set_facecolor("#060610")
        self.ax_hr.set_title("Heart Rate Trend", color="#ff6b6b", fontsize=10)
        self.ax_hr.set_ylabel("bpm", color="#888", fontsize=9)
        self.ax_hr.tick_params(colors="#666", labelsize=8)
        self.ax_hr.grid(True, alpha=0.12, color="#333")
        self.ax_hr.set_ylim(40, 160)
        self.hr_line, = self.ax_hr.plot([], [], color="#ff6b6b",
                                         linewidth=1.5, marker="o", markersize=3)

        self.ax_stat = self.fig.add_subplot(gs[2, 3])
        self.ax_stat.set_facecolor("#060610")
        self.ax_stat.axis("off")
        self._sv = {}
        rows = [("HR", "hr"), ("Temp", "temp"), ("Battery", "battery"),
                ("RR", "rr"), ("Pkts/s", "pkt_s")]
        for i, (lbl, key) in enumerate(rows):
            y = 0.90 - i * 0.18
            self.ax_stat.text(0.05, y, f"{lbl}:", color="#666", fontsize=9,
                              transform=self.ax_stat.transAxes)
            self._sv[key] = self.ax_stat.text(
                0.55, y, "--", color="#fff", fontsize=9, fontweight="bold",
                transform=self.ax_stat.transAxes)

        self.fig.text(0.5, 0.985,
                      "VSH101  Real-Time ECG Monitor  (PC Bluetooth)",
                      ha="center", color="#ddd", fontsize=11, fontweight="bold")
        self.fig.text(0.5, 0.962,
                      f"Intel Wireless BLE  |  {mac_str}  |  500 Hz",
                      ha="center", color="#445", fontsize=8)

        self.status_txt = self.fig.text(
            0.5, 0.075, "Connected  –  press START to measure",
            ha="center", color="#4488ff", fontsize=9)

        ax_s = self.fig.add_axes([0.27, 0.02, 0.14, 0.05])
        self.btn_start = Button(ax_s, "  START  ",
                                color="#0d2e0d", hovercolor="#1a5a1a")
        self.btn_start.label.set_color("#00ff88")
        self.btn_start.label.set_fontweight("bold")
        self.btn_start.on_clicked(self._on_start)

        ax_t = self.fig.add_axes([0.44, 0.02, 0.14, 0.05])
        self.btn_stop = Button(ax_t, "  STOP  ",
                               color="#2e0d0d", hovercolor="#5a1a1a")
        self.btn_stop.label.set_color("#ff6b6b")
        self.btn_stop.label.set_fontweight("bold")
        self.btn_stop.on_clicked(self._on_stop)

        ax_c = self.fig.add_axes([0.61, 0.02, 0.12, 0.05])
        self.btn_clear = Button(ax_c, "  CLEAR  ",
                                color="#0d0d2e", hovercolor="#1a1a5a")
        self.btn_clear.label.set_color("#88aaff")
        self.btn_clear.on_clicked(self._on_clear)

    def _on_start(self, _):
        if self.mgr.measuring:
            return
        self.mgr.measuring = True
        self.wait_text.set_visible(False)
        self._set_status("● Measuring...", "#00ff88")
        _log_msg("[UI] START")
        if self.vsc_thread_fn:
            threading.Thread(target=self.vsc_thread_fn, daemon=True).start()

    def _on_stop(self, _):
        if not self.mgr.measuring:
            return
        self.mgr.measuring = False
        self._set_status("■ Stopped", "#ff6b6b")
        _log_msg("[UI] STOP")

    def _on_clear(self, _):
        self.mgr.ecg_buffer.clear()
        self.mgr.hr_history.clear()
        self.ecg_line.set_data([], [])
        self.hr_line.set_data([], [])
        for v in self._sv.values():
            v.set_text("--")
        _log_msg("[UI] CLEAR")

    def _set_status(self, txt, color="#4488ff"):
        self.status_txt.set_text(txt)
        self.status_txt.set_color(color)

    def update(self, _frame):
        buf = list(self.mgr.ecg_buffer)
        abort_msg = self.mgr.status.get("abort")
        if abort_msg:
            self._set_status(f"✗ {abort_msg}", "#ff4444")
            self.mgr.status.pop("abort", None)
            self.wait_text.set_text("ACK failed — press CLEAR and try START again")
            self.wait_text.set_color("#ff4444")
            self.wait_text.set_visible(True)

        if buf and self.mgr.measuring:
            self.wait_text.set_visible(False)
            if len(buf) < DISPLAY_SAMPLES:
                buf = [0.0] * (DISPLAY_SAMPLES - len(buf)) + buf
            ecg = np.array(buf[-DISPLAY_SAMPLES:], dtype=np.float32)
            peak = np.percentile(np.abs(ecg), 99)
            if peak > 0.001:
                ecg = ecg / peak * 1.5
            self.ecg_line.set_data(self.t_axis, ecg)
            lo = "  [LEAD OFF]" if self.mgr.status["lead_off"] else ""
            self._set_status(
                f"● Measuring  |  Pkts: {self.mgr.packet_count}"
                f"  |  {self.mgr.status['pkt_s']} pkt/s{lo}",
                "#00ff88")

        hr_list = list(self.mgr.hr_history)
        if hr_list:
            self.hr_line.set_data(range(len(hr_list)), hr_list)
            self.ax_hr.set_xlim(0, max(len(hr_list) - 1, 10))
            self.ax_hr.set_ylim(max(30,  min(hr_list) - 10),
                                 min(220, max(hr_list) + 10))

        s = self.mgr.status
        self._sv["hr"].set_text(s["hr"])
        self._sv["temp"].set_text(s["temp"])
        self._sv["battery"].set_text(s["battery"])
        self._sv["rr"].set_text(s["rr"])
        self._sv["pkt_s"].set_text(str(s["pkt_s"]))
        return self.ecg_line, self.hr_line

    def start(self):
        self.ani = animation.FuncAnimation(
            self.fig, self.update, interval=200,
            blit=False, cache_frame_data=False)
        plt.show()


# ─────────────────────────────────────────────────────────────────
# Demo mode
# ─────────────────────────────────────────────────────────────────
def _fake_ecg(n, t_off, bpm=72.0):
    t   = np.arange(n) / SAMPLE_RATE + t_off
    rr  = 60.0 / bpm
    ecg = np.zeros(n)
    for i in range(int(t[-1] / rr) + 2):
        d    = t - i * rr
        ecg += 0.12 * np.exp(-((d - 0.10)**2) / (2 * 0.015**2))
        ecg -= 0.08 * np.exp(-((d - 0.155)**2) / (2 * 0.005**2))
        ecg += 1.10 * np.exp(-((d - 0.175)**2) / (2 * 0.006**2))
        ecg -= 0.12 * np.exp(-((d - 0.200)**2) / (2 * 0.006**2))
        ecg += 0.30 * np.exp(-((d - 0.300)**2) / (2 * 0.030**2))
    ecg += np.random.normal(0, 0.018, n)
    return list(ecg.astype(np.float32))

class DemoManager:
    mac          = "DEMO"
    vsc_type     = VSC_MODE_TYPE
    connected    = True
    measuring    = False
    packet_count = 0
    ecg_buffer   = deque(maxlen=DISPLAY_SAMPLES)
    hr_history   = deque(maxlen=120)
    status       = {"hr": "--", "temp": "--", "battery": "--",
                    "rr": "--", "pkt_s": 0.0, "lead_off": 0}

    def start_loop(self): pass
    def connect_sync(self, **_): return True
    def vsc_start(self):   pass
    def disconnect_sync(self): self.measuring = False

    def _demo_run(self):
        t_off = 0.0
        while True:
            if not self.measuring:
                time.sleep(0.05)
                continue
            bpm = 70 + 6 * np.sin(t_off / 8)
            self.ecg_buffer.extend(_fake_ecg(SAMPLES_PER_PKT, t_off, bpm))
            self.hr_history.append(bpm)
            self.packet_count += 1
            t_off += PACKET_INTERVAL
            self.status.update({
                "hr": f"{bpm:.0f} bpm", "temp": "36.5 C",
                "battery": "87%",       "rr": f"{60000/bpm:.0f} ms",
                "pkt_s": 5.0,
            })
            time.sleep(PACKET_INTERVAL)


# ─────────────────────────────────────────────────────────────────
# BLE scan helper
# ─────────────────────────────────────────────────────────────────
async def _scan(timeout=6.0):
    _log_msg(f"[SCAN] Scanning {timeout}s for BLE devices (PC Bluetooth)...")
    result = await BleakScanner.discover(timeout=timeout, return_adv=True)
    found = []
    for dev, adv in result.values():
        name = dev.name or "(unnamed)"
        rssi = getattr(adv, "rssi", "?")
        tag  = "  <-- VSH101" if "VSH101" in name else ""
        _log_msg(f"  {name:<34s}  {dev.address}  RSSI:{rssi:>5}{tag}")
        if "VSH101" in name:
            found.append(dev)
    return found


# ─────────────────────────────────────────────────────────────────
# Main 
# ─────────────────────────────────────────────────────────────────
def main():
    global DEFAULT_MAC

    parser = argparse.ArgumentParser(
        description="VSH101 ECG Real-Time Monitor  (PC Bluetooth / Intel Wireless)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python vsh101_ble.py
  python vsh101_ble.py --mac CC:CC:CC:90:BA:2B
  python vsh101_ble.py --scan-only
  python vsh101_ble.py --demo
        """
    )
    parser.add_argument("--mac",          default=DEFAULT_MAC,
                        help="VSH101 BLE MAC address (omitted to trigger auto-scan menu)")
    parser.add_argument("--type",         default=VSC_MODE_TYPE, type=int, choices=[0, 1],
                        help="VSC Mode 0=2ch/968B  1=1ch/568B (default: 1)")
    parser.add_argument("--scan-only",    action="store_true",
                        help="Scan for VSH101 devices and exit")
    parser.add_argument("--scan-timeout", default=6.0, type=float,
                        help="Scan timeout seconds (default: 6)")
    parser.add_argument("--demo",         action="store_true",
                        help="Simulate ECG (no hardware needed)")
    args = parser.parse_args()

    # ── Scan only ─────────────────────────────────────────────────
    if args.scan_only:
        found = asyncio.run(_scan(args.scan_timeout))
        _log_msg(f"\nFound {len(found)} VSH101 device(s).")
        return

    # ── Demo mode ─────────────────────────────────────────────────
    if args.demo:
        _log_msg("[DEMO] Simulation mode — no hardware needed")
        demo = DemoManager()
        threading.Thread(target=demo._demo_run, daemon=True).start()

        def _demo_vsc():
            pass

        plotter = ECGPlotter(demo, vsc_thread_fn=_demo_vsc)
        plotter.fig.canvas.mpl_connect("close_event",
                                        lambda e: demo.disconnect_sync())
        plotter.start()
        return

    # ── Device selection and connection logic ────
    target_mac = args.mac
    if not target_mac:
        # No --mac specified on command line — trigger auto-scan
        devices = asyncio.run(_scan(args.scan_timeout))
        if not devices:
            _log_msg("\n[WARN] No VSH101 device found. Ensure device is powered on.")
            return
        
        if len(devices) == 1:
            target_mac = devices[0].address
            _log_msg(f"\n[MAIN] Automatically selected device: {devices[0].name} ({target_mac})")
        else:
            _log_msg("\nMultiple devices found, please choose:")
            for i, d in enumerate(devices):
                _log_msg(f"  [{i}] {d.name:<24s} ({d.address})")
            try:
                idx = int(input("Enter index to connect: ").strip())
                target_mac = devices[idx].address
            except (ValueError, IndexError):
                _log_msg("[ERROR] Invalid selection. Exiting.")
                return

    # ── Real BLE mode ─────────────────────────────────────────────
    _log_msg("=" * 60)
    _log_msg("  VSH101 ECG Real-Time Monitor  (PC Bluetooth)")
    _log_msg(f"  Adapter  : Intel(R) Wireless Bluetooth")
    _log_msg(f"  MAC      : {target_mac}")
    _log_msg(f"  VSC Type : {args.type}")
    _log_msg(f"  Write    : {BLE_WRITE_UUID}")
    _log_msg(f"  Notify   : {BLE_NOTIFY_UUID}")
    _log_msg("=" * 60)

    mgr = BLEManager(mac=target_mac, vsc_type=args.type)

    # Start dedicated BLE event loop thread
    mgr.start_loop()

    # Connect (blocks until done or 25 s timeout)
    _log_msg(f"[MAIN] Connecting to {target_mac} ...")
    if not mgr.connect_sync(timeout=25.0):
        _log_msg("[FATAL] BLE connection failed.")
        _log_msg("  - Is VSH101 powered on?  (hold button 5 s, green LED blinks)")
        _log_msg("  - Is PC Bluetooth enabled?")
        _log_msg("  - Verify MAC with:  python vsh101_ble.py --scan-only")
        return

    _log_msg("[MAIN] Opening ECG window...")

    def _vsc_worker():
        """Called from START button — submits VSC loop to BLE event loop."""
        mgr.vsc_start()

    plotter = ECGPlotter(mgr, vsc_thread_fn=_vsc_worker)

    def _on_close(_event):
        _log_msg("[INFO] Window closed")
        mgr.measuring = False
        threading.Thread(target=mgr.disconnect_sync, daemon=True).start()

    plotter.fig.canvas.mpl_connect("close_event", _on_close)
    plotter.start()   # blocks until window closed


if __name__ == "__main__":
    import sys
    main()