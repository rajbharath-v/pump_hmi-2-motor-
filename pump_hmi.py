"""
=============================================================================
SHENCHEN OEM-STB PERISTALTIC PUMP — PROFESSIONAL HMI
=============================================================================
Pump    : Shenchen OEM-STB
Control : MODBUS RTU over RS485
Registers:
    1000 -> Start/Stop  (0=Stop, 1=Start)
    1001 -> Direction   (0=Forward, 1=Reverse)
    1002 -> Speed RPM   (32-bit float, registers 1002+1003)

Flow Rates (at max 350 RPM, from official datasheet):
    Tube 1x1mm -> 14.88 mL/min
    Tube 2x1mm -> 50.05 mL/min
    Tube 3x1mm -> 110.27 mL/min
    Tube 4x1mm -> 149.23 mL/min

Volume calculation:
    flow_rate = (max_flow / 350) * rpm
    run_time  = (volume_ml / flow_rate) * 60  (seconds)

Run:
    python pump_hmi.py
=============================================================================
"""

import sys
import os
import time
import threading
import struct
import logging
import json
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont

# ── Auto-detect COM port ────────────────────────────────────────────────────
def auto_detect_port():
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        for p in ports:
            desc = (p.description or "").upper()
            if any(k in desc for k in ["CP210", "CH340", "FTDI", "USB", "UART"]):
                return p.device
        if ports:
            return ports[0].device
    except Exception:
        pass
    return "COM9"

# ── Shared MODBUS Client — ONE port, multiple slave IDs ────────────────────
class SharedModbusClient:
    """
    Single RS485 serial connection shared by all pump channels.
    RS485 is a BUS — one COM port talks to multiple slaves via slave ID.
    """
    _instance = None

    def __init__(self):
        self._client    = None
        self._connected = False
        self._lock      = threading.Lock()
        self._port      = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls):
        if cls._instance:
            try: cls._instance._client.close()
            except: pass
        cls._instance = None

    def connect(self, port, baudrate=9600, parity="E", stopbits=1):
        from pymodbus.client import ModbusSerialClient
        import io, sys

        # Close existing
        try:
            if self._client:
                self._client.close()
        except: pass
        self._client    = None
        self._connected = False
        self._port      = port

        # Retry 3 times with delay
        for attempt in range(3):
            try:
                # Suppress pymodbus stderr noise
                old_stderr = sys.stderr
                sys.stderr = io.StringIO()
                try:
                    self._client = ModbusSerialClient(
                        port=port, baudrate=baudrate,
                        parity=parity, stopbits=stopbits,
                        bytesize=8, timeout=1.5,
                    )
                    ok = self._client.connect()
                finally:
                    sys.stderr = old_stderr

                if ok:
                    self._connected = True
                    time.sleep(0.3)
                    return True
                else:
                    time.sleep(0.5)
            except Exception:
                sys.stderr = old_stderr if 'old_stderr' in dir() else sys.stderr
                time.sleep(0.5)

        self._connected = False
        return False

    def disconnect(self):
        self._connected = False
        try:
            if self._client:
                self._client.close()
        except: pass
        self._client = None
        SharedModbusClient._instance = None

    def is_connected(self):
        return self._connected

    def write_register(self, address, value, slave):
        with self._lock:
            if not self._connected or not self._client:
                return False
            try:
                r = self._client.write_register(
                    address=address, value=value, slave=slave)
                if r.isError():
                    return False
                return True
            except Exception:
                self._connected = False  # cable unplugged!
                return False

    def write_float(self, address, value, slave):
        with self._lock:
            if not self._connected or not self._client:
                return False
            try:
                raw = struct.pack(">f", float(value))
                hi  = struct.unpack(">H", raw[0:2])[0]
                lo  = struct.unpack(">H", raw[2:4])[0]
                r   = self._client.write_registers(
                    address=address, values=[hi, lo], slave=slave)
                if r.isError():
                    return False
                return True
            except Exception:
                self._connected = False  # cable unplugged!
                return False


# ── MODBUS Driver ───────────────────────────────────────────────────────────
class PumpDriver:
    def __init__(self, slave_id=1):
        self.slave_id   = slave_id
        self._connected = False
        self._running   = False
        self._direction = 0
        self._speed     = 0.0

    def connect(self):
        self._connected = SharedModbusClient.get().is_connected()
        return self._connected

    def disconnect(self):
        self._connected = False

    def is_connected(self):
        return self._connected and SharedModbusClient.get().is_connected()

    def _write_reg(self, addr, val):
        return SharedModbusClient.get().write_register(addr, val, self.slave_id)

    def _write_float(self, addr, val):
        return SharedModbusClient.get().write_float(addr, val, self.slave_id)

    def set_speed(self, rpm):
        rpm = max(0.1, min(float(rpm), 350.0))
        ok  = self._write_float(1002, rpm)
        if ok:
            self._speed = rpm
        return ok

    def start(self):
        ok = self._write_reg(1000, 1)
        self._running = True   # mark running even if write fails
        return ok

    def stop(self):
        # Try 3 times to guarantee stop
        for _ in range(3):
            try:
                self._write_reg(1000, 0)
            except: pass
        self._running = False  # always mark stopped
        return True

    def set_direction(self, forward=True):
        val = 0 if forward else 1
        ok  = self._write_reg(1001, val)
        if ok:
            self._direction = val
        return ok

    @property
    def is_running(self):
        return self._running

    @property
    def speed(self):
        return self._speed

    @property
    def direction_str(self):
        return "CW" if self._direction == 0 else "CCW"


# ── Flow Rate Calculator ────────────────────────────────────────────────────
TUBE_DATA = {
    "1x1mm": {"label": "1x1 mm", "max_flow": 14.88},
    "2x1mm": {"label": "2x1 mm", "max_flow": 50.05},
    "3x1mm": {"label": "3x1 mm", "max_flow": 110.27},
    "4x1mm": {"label": "4x1 mm", "max_flow": 149.23},
}
MAX_RPM = 350.0

def calc_flow_rate(tube_key, rpm):
    """mL/min at given RPM"""
    max_flow = TUBE_DATA[tube_key]["max_flow"]
    return (max_flow / MAX_RPM) * rpm

def calc_run_time(tube_key, rpm, volume_ml):
    """seconds needed to dispense volume_ml"""
    flow = calc_flow_rate(tube_key, rpm)
    if flow <= 0:
        return 0
    return (volume_ml / flow) * 60.0

def calc_volume(tube_key, rpm, seconds):
    """mL dispensed in given seconds"""
    flow = calc_flow_rate(tube_key, rpm)
    return flow * (seconds / 60.0)


# ── Settings persistence ────────────────────────────────────────────────────
SETTINGS_FILE = Path("pump_settings.json")

def load_settings():
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        pass
    return {}

def save_settings(data):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ============================================================================
# PROFESSIONAL HMI — TKINTER APPLICATION
# ============================================================================

# ── Color Palette (industrial blue/white theme like the real pump) ───────────
C = {
    "bg":       "#f0f4f8",
    "panel":    "#ffffff",
    "header":   "#1565C0",
    "header2":  "#1976D2",
    "accent":   "#0288D1",
    "accent2":  "#00ACC1",
    "green":    "#2E7D32",
    "green_lt": "#43A047",
    "red":      "#C62828",
    "red_lt":   "#E53935",
    "orange":   "#E65100",
    "text":     "#1a2744",
    "text_dim": "#546e7a",
    "text_lt":  "#ffffff",
    "border":   "#B0BEC5",
    "input_bg": "#1a2744",
    "input_fg": "#ffffff",
    "row_alt":  "#e8f4fd",
    "cyan":     "#006064",
    "teal":     "#00838F",
}

class PumpHMI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Shenchen Pump Control System")
        self.geometry("1024x768")
        self.minsize(900, 650)
        self.configure(bg=C["bg"])
        self.resizable(True, True)

        # State
        self.pump1 = None
        self.pump2 = None
        self._settings = load_settings()
        self._stop_events = {}
        self._threads    = {}
        self._disp_volume = [tk.DoubleVar(value=0.0), tk.DoubleVar(value=0.0)]
        self._total_vol   = [0.0, 0.0]

        # Settings vars
        self._port_var    = tk.StringVar(value=self._settings.get("port", auto_detect_port()))
        self._tube_var    = [
            tk.StringVar(value=self._settings.get("tube1", "2x1mm")),
            tk.StringVar(value=self._settings.get("tube2", "2x1mm")),
        ]
        self._slave_var   = [
            tk.IntVar(value=self._settings.get("slave1", 1)),
            tk.IntVar(value=self._settings.get("slave2", 2)),
        ]
        self._calib_factor = [
            tk.DoubleVar(value=self._settings.get("calib1", 1.0)),
            tk.DoubleVar(value=self._settings.get("calib2", 1.0)),
        ]
        self._suckback_var = [
            tk.DoubleVar(value=self._settings.get("suckback1", 0.0)),
            tk.DoubleVar(value=self._settings.get("suckback2", 0.0)),
        ]

        # Build UI
        self._build_fonts()
        self._build_ui()
        self._start_clock()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Fonts
    # ------------------------------------------------------------------
    def _build_fonts(self):
        self.f_title  = ("Segoe UI", 14, "bold")
        self.f_head   = ("Segoe UI", 11, "bold")
        self.f_label  = ("Segoe UI", 10)
        self.f_bold   = ("Segoe UI", 10, "bold")
        self.f_big    = ("Segoe UI", 28, "bold")
        self.f_med    = ("Segoe UI", 16, "bold")
        self.f_mono   = ("Consolas", 11)
        self.f_small  = ("Segoe UI", 9)
        self.f_btn    = ("Segoe UI", 11, "bold")

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        # ── Header ──────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["header"], height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        tk.Label(hdr, text="  SHENCHEN PUMP CONTROL SYSTEM",
                 font=self.f_title, bg=C["header"], fg=C["text_lt"]).pack(side="left", padx=10)

        self._clock_lbl = tk.Label(hdr, text="", font=("Consolas", 13),
                                   bg=C["header"], fg="#90CAF9")
        self._clock_lbl.pack(side="right", padx=20)

        self._conn_lbl = tk.Label(hdr, text="● DISCONNECTED",
                                  font=("Segoe UI", 10, "bold"),
                                  bg=C["header"], fg="#FF8A80")
        self._conn_lbl.pack(side="right", padx=10)

        # ── Notebook (tabs) ─────────────────────────────────────────────
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook",        background=C["bg"], borderwidth=0)
        style.configure("TNotebook.Tab",    font=self.f_bold,
                        padding=[16, 8], background=C["border"], foreground=C["text"])
        style.map("TNotebook.Tab",
                  background=[("selected", C["accent"])],
                  foreground=[("selected", "white")])
        style.configure("TProgressbar", troughcolor=C["bg"],
                        background=C["accent"], thickness=10)
        style.configure("Treeview",           font=self.f_label,
                        background=C["panel"], fieldbackground=C["panel"],
                        rowheight=28)
        style.configure("Treeview.Heading",   font=self.f_bold,
                        background=C["accent"], foreground="white")

        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=0, pady=0)

        self._build_tab_dashboard()
        self._build_tab_dispensing()
        self._build_tab_timing()
        self._build_tab_calibration()
        self._build_tab_common_mode()
        self._build_tab_settings()

    # ------------------------------------------------------------------
    # Helper widgets
    # ------------------------------------------------------------------
    def _tab_frame(self, label):
        f = tk.Frame(self._nb, bg=C["bg"])
        self._nb.add(f, text=f"  {label}  ")
        return f

    def _card(self, parent, title="", colspan=1):
        outer = tk.Frame(parent, bg=C["bg"], padx=6, pady=6)
        inner = tk.Frame(outer, bg=C["panel"],
                         relief="flat", bd=0,
                         highlightbackground=C["border"],
                         highlightthickness=1)
        inner.pack(fill="both", expand=True)
        if title:
            hdr = tk.Frame(inner, bg=C["accent"], height=30)
            hdr.pack(fill="x")
            hdr.pack_propagate(False)
            tk.Label(hdr, text=f"  {title}", font=self.f_bold,
                     bg=C["accent"], fg="white").pack(side="left", pady=4)
        body = tk.Frame(inner, bg=C["panel"], padx=10, pady=8)
        body.pack(fill="both", expand=True)
        return outer, body

    def _input_box(self, parent, label, var, unit="", width=10, row=0, col=0):
        tk.Label(parent, text=label, font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).grid(
                     row=row, column=col, sticky="w", pady=3, padx=(0, 4))
        fr = tk.Frame(parent, bg=C["input_bg"], padx=4, pady=3)
        fr.grid(row=row, column=col+1, sticky="w", pady=3, padx=4)
        e = tk.Entry(fr, textvariable=var, font=("Consolas", 13, "bold"),
                     bg=C["input_bg"], fg=C["input_fg"],
                     insertbackground="white", bd=0, width=width)
        e.pack(side="left")
        if unit:
            tk.Label(fr, text=f" {unit}", font=self.f_small,
                     bg=C["input_bg"], fg="#90CAF9").pack(side="left")
        return e

    def _toggle_btn(self, parent, text_on, text_off, var, cmd=None,
                    col_on=None, col_off=None, **kwargs):
        col_on  = col_on  or C["green"]
        col_off = col_off or C["red"]
        def _toggle():
            var.set(not var.get())
            _update()
            if cmd:
                cmd()
        def _update():
            if var.get():
                b.config(text=text_on, bg=col_on)
            else:
                b.config(text=text_off, bg=col_off)
        b = tk.Button(parent, font=self.f_btn, fg="white",
                      relief="flat", padx=14, pady=8,
                      command=_toggle, **kwargs)
        _update()
        return b

    def _big_btn(self, parent, text, cmd, color, **kwargs):
        return tk.Button(parent, text=text, command=cmd,
                         font=self.f_btn, bg=color, fg="white",
                         relief="flat", padx=16, pady=10,
                         activebackground=color, cursor="hand2", **kwargs)

    def _value_display(self, parent, label, var, unit, color=None):
        color = color or C["input_bg"]
        f = tk.Frame(parent, bg=C["panel"])
        f.pack(fill="x", pady=4)
        tk.Label(f, text=label, font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"], width=14, anchor="w").pack(side="left")
        box = tk.Frame(f, bg=color, padx=8, pady=4)
        box.pack(side="left")
        tk.Label(box, textvariable=var, font=("Consolas", 14, "bold"),
                 bg=color, fg="white", width=10, anchor="e").pack(side="left")
        tk.Label(box, text=f" {unit}", font=self.f_small,
                 bg=color, fg="#90CAF9").pack(side="left")

    # ------------------------------------------------------------------
    # TAB 1 — DASHBOARD
    # ------------------------------------------------------------------
    def _build_tab_dashboard(self):
        tab = self._tab_frame("Dashboard")

        # Two motor panels side by side
        motors = tk.Frame(tab, bg=C["bg"])
        motors.pack(fill="both", expand=True, padx=4, pady=4)
        motors.columnconfigure(0, weight=1)
        motors.columnconfigure(1, weight=1)
        motors.rowconfigure(0, weight=1)

        self._motor_panels = []
        for i in range(2):
            outer, body = self._card(motors, f"PUMP CHANNEL {i+1}  (Slave {i+1})")
            outer.grid(row=0, column=i, sticky="nsew")
            self._build_motor_panel(body, i)

        # Bottom status bar
        status = tk.Frame(tab, bg=C["header2"], height=36)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        tk.Label(status,
                 text="  OEM-STB Series  |  MODBUS RTU RS485  |  Shenchen Precision Pump",
                 font=self.f_small, bg=C["header2"], fg="#B3E5FC").pack(side="left", pady=8)

    def _build_motor_panel(self, parent, idx):
        # Status indicator
        top = tk.Frame(parent, bg=C["panel"])
        top.pack(fill="x", pady=(0, 8))

        self._status_dot = [None, None]
        dot = tk.Label(top, text="●", font=("Segoe UI", 20),
                       bg=C["panel"], fg=C["red"])
        dot.pack(side="left")
        self._status_dot[idx] = dot

        self._status_txt = getattr(self, "_status_txt", [None, None])
        self._status_txt[idx] = tk.Label(top, text="STOPPED",
                                          font=("Segoe UI", 12, "bold"),
                                          bg=C["panel"], fg=C["red"])
        self._status_txt[idx].pack(side="left", padx=6)

        self._dir_lbl = getattr(self, "_dir_lbl", [None, None])
        self._dir_lbl[idx] = tk.Label(top, text="CW",
                                       font=self.f_small,
                                       bg=C["accent2"], fg="white",
                                       padx=6, pady=2)
        self._dir_lbl[idx].pack(side="right", padx=4)

        # Volume display
        vol_fr = tk.Frame(parent, bg=C["input_bg"], padx=12, pady=10)
        vol_fr.pack(fill="x", pady=4)
        tk.Label(vol_fr, text="DISPENSED VOLUME",
                 font=self.f_small, bg=C["input_bg"], fg="#90CAF9").pack()
        self._disp_volume[idx] = tk.DoubleVar(value=0.0)
        tk.Label(vol_fr, textvariable=self._disp_volume[idx],
                 font=("Consolas", 36, "bold"),
                 bg=C["input_bg"], fg="#64FFDA").pack()
        tk.Label(vol_fr, text="mL",
                 font=("Segoe UI", 14),
                 bg=C["input_bg"], fg="#90CAF9").pack()

        # RPM display
        rpm_fr = tk.Frame(parent, bg=C["panel"])
        rpm_fr.pack(fill="x", pady=2)
        tk.Label(rpm_fr, text="Speed:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"], width=10, anchor="w").pack(side="left")
        self._rpm_lbl = getattr(self, "_rpm_lbl", [None, None])
        self._rpm_lbl[idx] = tk.Label(rpm_fr, text="0.00 RPM",
                                       font=("Consolas", 13, "bold"),
                                       bg=C["panel"], fg=C["text"])
        self._rpm_lbl[idx].pack(side="left")

        # Tube info
        tube_fr = tk.Frame(parent, bg=C["panel"])
        tube_fr.pack(fill="x", pady=2)
        tk.Label(tube_fr, text="Tube:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"], width=10, anchor="w").pack(side="left")
        self._tube_lbl = getattr(self, "_tube_lbl", [None, None])
        self._tube_lbl[idx] = tk.Label(tube_fr, text="--",
                                        font=self.f_label,
                                        bg=C["panel"], fg=C["text"])
        self._tube_lbl[idx].pack(side="left")

        # Flow rate
        flow_fr = tk.Frame(parent, bg=C["panel"])
        flow_fr.pack(fill="x", pady=2)
        tk.Label(flow_fr, text="Flow Rate:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"], width=10, anchor="w").pack(side="left")
        self._flow_lbl = getattr(self, "_flow_lbl", [None, None])
        self._flow_lbl[idx] = tk.Label(flow_fr, text="-- mL/min",
                                        font=self.f_label,
                                        bg=C["panel"], fg=C["text"])
        self._flow_lbl[idx].pack(side="left")

        sep = tk.Frame(parent, bg=C["border"], height=1)
        sep.pack(fill="x", pady=8)

        # RPM Slider
        tk.Label(parent, text="Speed Control (RPM):", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")

        self._rpm_var = getattr(self, "_rpm_var", [None, None])
        self._rpm_var[idx] = tk.DoubleVar(value=60.0)

        rpm_row = tk.Frame(parent, bg=C["panel"])
        rpm_row.pack(fill="x", pady=2)

        sl = tk.Scale(rpm_row, from_=0.1, to=350, orient="horizontal",
                      variable=self._rpm_var[idx], resolution=0.1,
                      bg=C["panel"], fg=C["text"], troughcolor=C["bg"],
                      highlightthickness=0, length=200,
                      command=lambda v, i=idx: self._on_rpm_change(i))
        sl.pack(side="left", fill="x", expand=True)

        rpm_entry = tk.Entry(rpm_row, textvariable=self._rpm_var[idx],
                             font=("Consolas", 11), bg=C["input_bg"],
                             fg="white", insertbackground="white",
                             width=7, bd=0)
        rpm_entry.pack(side="left", padx=6)
        tk.Label(rpm_row, text="RPM", font=self.f_small,
                 bg=C["panel"], fg=C["text_dim"]).pack(side="left")

        # Quick RPM presets
        preset_fr = tk.Frame(parent, bg=C["panel"])
        preset_fr.pack(fill="x", pady=4)
        for v in [10, 30, 60, 100, 150, 200, 300]:
            tk.Button(preset_fr, text=str(v),
                      command=lambda val=v, i=idx: self._set_rpm(i, float(val)),
                      font=("Segoe UI", 9, "bold"), bg=C["accent2"], fg="white",
                      relief="flat", padx=8, pady=4,
                      cursor="hand2").pack(side="left", padx=1)

        sep2 = tk.Frame(parent, bg=C["border"], height=1)
        sep2.pack(fill="x", pady=6)

        # Direction
        dir_row = tk.Frame(parent, bg=C["panel"])
        dir_row.pack(fill="x", pady=2)
        tk.Label(dir_row, text="Direction:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(side="left")

        self._dir_var = getattr(self, "_dir_var", [None, None])
        self._dir_var[idx] = tk.StringVar(value="CW")

        tk.Radiobutton(dir_row, text="CW (Forward)",
                       variable=self._dir_var[idx], value="CW",
                       command=lambda i=idx: self._set_direction(i),
                       bg=C["panel"], fg=C["text"],
                       selectcolor=C["bg"],
                       activebackground=C["panel"]).pack(side="left", padx=8)
        tk.Radiobutton(dir_row, text="CCW (Reverse)",
                       variable=self._dir_var[idx], value="CCW",
                       command=lambda i=idx: self._set_direction(i),
                       bg=C["panel"], fg=C["text"],
                       selectcolor=C["bg"],
                       activebackground=C["panel"]).pack(side="left")

        # START / STOP buttons
        btn_row = tk.Frame(parent, bg=C["panel"])
        btn_row.pack(fill="x", pady=8)

        self._big_btn(btn_row, "  START",
                      lambda i=idx: self._cmd_start(i),
                      C["green"], width=10).pack(side="left", padx=(0, 8))

        self._big_btn(btn_row, "  STOP",
                      lambda i=idx: self._cmd_stop(i),
                      C["red"], width=10).pack(side="left", padx=(0, 8))

        self._big_btn(btn_row, "  RESET VOL",
                      lambda i=idx: self._reset_volume(i),
                      C["teal"], width=10).pack(side="left")

    # ------------------------------------------------------------------
    # TAB 2 — DISPENSING
    # ------------------------------------------------------------------
    def _build_tab_dispensing(self):
        tab = self._tab_frame("Dispensing")

        top = tk.Frame(tab, bg=C["bg"])
        top.pack(fill="both", expand=True, padx=4, pady=4)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        self._disp_panels = []
        for i in range(2):
            outer, body = self._card(top, f"CHANNEL {i+1}  DISPENSING")
            outer.grid(row=0, column=i, sticky="nsew", padx=4)
            self._build_dispensing_panel(body, i)

    def _build_dispensing_panel(self, parent, idx):
        # Mode selection
        mode_fr = tk.Frame(parent, bg=C["panel"])
        mode_fr.pack(fill="x", pady=(0, 8))

        self._disp_mode = getattr(self, "_disp_mode", [None, None])
        self._disp_mode[idx] = tk.StringVar(value="volume")

        for val, label in [("volume", "Volume Disp."), ("speed", "Speed Disp.")]:
            rb = tk.Radiobutton(mode_fr, text=label,
                                variable=self._disp_mode[idx], value=val,
                                font=self.f_bold,
                                bg=C["accent"] if val=="volume" else C["bg"],
                                fg="white" if val=="volume" else C["text"],
                                selectcolor=C["accent"],
                                activebackground=C["accent"],
                                relief="flat", padx=12, pady=6,
                                indicatoron=False,
                                command=lambda i=idx: self._update_disp_mode(i))
            rb.pack(side="left", padx=(0, 4))

        sep = tk.Frame(parent, bg=C["border"], height=1)
        sep.pack(fill="x", pady=6)

        # Input fields
        grid = tk.Frame(parent, bg=C["panel"])
        grid.pack(fill="x", pady=4)

        self._dv_vol   = getattr(self, "_dv_vol",  [None, None])
        self._dv_time  = getattr(self, "_dv_time", [None, None])
        self._dv_pause = getattr(self, "_dv_pause",[None, None])
        self._dv_rep   = getattr(self, "_dv_rep",  [None, None])
        self._dv_speed = getattr(self, "_dv_speed",[None, None])

        self._dv_vol[idx]   = tk.DoubleVar(value=10.0)
        self._dv_time[idx]  = tk.DoubleVar(value=2.0)
        self._dv_pause[idx] = tk.DoubleVar(value=1.0)
        self._dv_rep[idx]   = tk.IntVar(value=1)
        self._dv_speed[idx] = tk.DoubleVar(value=60.0)

        self._input_box(grid, "Disp. Vol.:", self._dv_vol[idx],  "mL", row=0, col=0)
        self._input_box(grid, "Disp. Time:", self._dv_time[idx], "s",  row=1, col=0)
        self._input_box(grid, "Pause Time:", self._dv_pause[idx],"s",  row=2, col=0)
        self._input_box(grid, "Repeat:",     self._dv_rep[idx],  "",   row=3, col=0)
        self._input_box(grid, "Speed:",      self._dv_speed[idx],"RPM",row=4, col=0)

        # RPM Slider for dispensing
        sep_sl = tk.Frame(parent, bg=C["border"], height=1)
        sep_sl.pack(fill="x", pady=4)
        tk.Label(parent, text="Speed Control:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")

        sl_row = tk.Frame(parent, bg=C["panel"])
        sl_row.pack(fill="x", pady=2)
        sl = tk.Scale(sl_row, from_=0.1, to=350, orient="horizontal",
                      variable=self._dv_speed[idx], resolution=0.1,
                      bg=C["panel"], fg=C["text"],
                      troughcolor=C["bg"], highlightthickness=0,
                      length=200,
                      command=lambda v, i=idx: self._on_disp_speed_change(i))
        sl.pack(side="left", fill="x", expand=True)

        spd_box = tk.Frame(sl_row, bg=C["input_bg"], padx=6, pady=3)
        spd_box.pack(side="left", padx=4)
        tk.Label(spd_box, textvariable=self._dv_speed[idx],
                 font=("Consolas", 11, "bold"),
                 bg=C["input_bg"], fg="white", width=6).pack(side="left")
        tk.Label(spd_box, text=" RPM", font=self.f_small,
                 bg=C["input_bg"], fg="#90CAF9").pack(side="left")

        # Speed presets for dispensing
        pr_row = tk.Frame(parent, bg=C["panel"])
        pr_row.pack(fill="x", pady=2)
        for v in [10, 30, 60, 100, 150, 200, 300]:
            tk.Button(pr_row, text=str(v),
                      command=lambda val=v, i=idx: self._set_disp_speed(i, float(val)),
                      font=("Segoe UI", 9, "bold"),
                      bg=C["accent2"], fg="white",
                      relief="flat", padx=6, pady=3,
                      cursor="hand2").pack(side="left", padx=1)

        # Calculated run time info
        calc_fr = tk.Frame(parent, bg=C["row_alt"], padx=8, pady=6)
        calc_fr.pack(fill="x", pady=4)
        self._calc_time_lbl = getattr(self, "_calc_time_lbl", [None, None])
        self._calc_time_lbl[idx] = tk.Label(calc_fr,
                                             text="Set volume and speed to see run time",
                                             font=self.f_small,
                                             bg=C["row_alt"], fg=C["text_dim"])
        self._calc_time_lbl[idx].pack()

        # Bind vol/speed changes to update calculated time
        self._dv_vol[idx].trace_add("write",
            lambda *a, i=idx: self._update_calc_time(i))
        self._dv_speed[idx].trace_add("write",
            lambda *a, i=idx: self._update_calc_time(i))

        sep2 = tk.Frame(parent, bg=C["border"], height=1)
        sep2.pack(fill="x", pady=6)

        # Progress
        self._disp_prog = getattr(self, "_disp_prog", [None, None])
        self._disp_prog[idx] = ttk.Progressbar(parent, mode="determinate", length=300)
        self._disp_prog[idx].pack(fill="x", pady=4)

        self._disp_status = getattr(self, "_disp_status", [None, None])
        self._disp_status[idx] = tk.Label(parent, text="Ready",
                                           font=self.f_mono,
                                           bg=C["panel"], fg=C["text_dim"])
        self._disp_status[idx].pack(pady=2)

        self._disp_counter = getattr(self, "_disp_counter", [None, None])
        self._disp_counter[idx] = tk.Label(parent, text="0 / 0",
                                            font=("Segoe UI", 12, "bold"),
                                            bg=C["panel"], fg=C["accent"])
        self._disp_counter[idx].pack(pady=2)

        # Buttons
        btn_row = tk.Frame(parent, bg=C["panel"])
        btn_row.pack(fill="x", pady=6)

        self._big_btn(btn_row, "  START",
                      lambda i=idx: self._run_dispense(i),
                      C["green"]).pack(side="left", padx=(0, 6))

        self._big_btn(btn_row, "  STOP",
                      lambda i=idx: self._stop_dispense(i),
                      C["red"]).pack(side="left")

        # Calculated info
        info_fr = tk.Frame(parent, bg=C["row_alt"], padx=8, pady=6)
        info_fr.pack(fill="x", pady=6)
        tk.Label(info_fr, text="Calculated run time will show here",
                 font=self.f_small, bg=C["row_alt"], fg=C["text_dim"]).pack()

    def _update_disp_mode(self, idx):
        pass  # mode toggle visual update

    # ------------------------------------------------------------------
    # TAB 3 — TIMING
    # ------------------------------------------------------------------
    def _build_tab_timing(self):
        tab = self._tab_frame("Timing")

        fr = tk.Frame(tab, bg=C["bg"])
        fr.pack(fill="both", expand=True, padx=8, pady=8)
        fr.columnconfigure(0, weight=1)
        fr.columnconfigure(1, weight=1)

        for i in range(2):
            outer, body = self._card(fr, f"CHANNEL {i+1}  TIMING START/STOP")
            outer.grid(row=0, column=i, sticky="nsew", padx=4)
            self._build_timing_panel(body, i)

    def _build_timing_panel(self, parent, idx):
        # Start time section
        for section, label in [("start", "Start Time"), ("stop", "Stop Time")]:
            sec_fr = tk.Frame(parent, bg=C["row_alt" if section=="start" else "panel"],
                              padx=10, pady=8)
            sec_fr.pack(fill="x", pady=4)

            tk.Label(sec_fr, text=label, font=self.f_head,
                     bg=sec_fr["bg"], fg=C["accent"]).pack(anchor="w")

            time_fr = tk.Frame(sec_fr, bg=sec_fr["bg"])
            time_fr.pack(anchor="w", pady=4)

            # HH:MM:SS inputs
            key = f"_timing_{section}_{idx}"
            h_var = tk.IntVar(value=0)
            m_var = tk.IntVar(value=0)
            s_var = tk.IntVar(value=0)
            setattr(self, f"{key}_h", h_var)
            setattr(self, f"{key}_m", m_var)
            setattr(self, f"{key}_s", s_var)

            for var, lbl in [(h_var, "HH"), (m_var, "MM"), (s_var, "SS")]:
                box = tk.Frame(time_fr, bg=C["input_bg"], padx=6, pady=4)
                box.pack(side="left", padx=2)
                tk.Entry(box, textvariable=var, font=("Consolas", 16, "bold"),
                         bg=C["input_bg"], fg="white",
                         insertbackground="white", bd=0, width=3).pack()
                tk.Label(time_fr, text=lbl, font=self.f_small,
                         bg=sec_fr["bg"], fg=C["text_dim"]).pack(side="left", padx=(0, 4))

            # Enable toggle
            en_var = tk.BooleanVar(value=False)
            setattr(self, f"{key}_en", en_var)

            tog_fr = tk.Frame(sec_fr, bg=sec_fr["bg"])
            tog_fr.pack(anchor="w", pady=4)

            en_btn = tk.Button(tog_fr, text="OFF", font=self.f_bold,
                               bg=C["red"], fg="white", relief="flat",
                               padx=14, pady=4)
            en_btn.pack(side="left")

            def make_toggle(btn, var):
                def toggle():
                    var.set(not var.get())
                    btn.config(text="ON" if var.get() else "OFF",
                               bg=C["green"] if var.get() else C["red"])
                return toggle
            en_btn.config(command=make_toggle(en_btn, en_var))

            # Once / Custom
            freq_var = tk.StringVar(value="once")
            setattr(self, f"{key}_freq", freq_var)
            for val, lbl in [("once", "Once"), ("custom", "Custom")]:
                tk.Radiobutton(sec_fr, text=lbl, variable=freq_var, value=val,
                               font=self.f_label, bg=sec_fr["bg"], fg=C["text"],
                               selectcolor=C["bg"],
                               activebackground=sec_fr["bg"]).pack(anchor="w")

        # OK button
        btn_row = tk.Frame(parent, bg=C["panel"])
        btn_row.pack(fill="x", pady=8)
        self._big_btn(btn_row, "  APPLY",
                      lambda i=idx: self._apply_timing(i),
                      C["accent"]).pack(side="right")

    def _apply_timing(self, idx):
        messagebox.showinfo("Timing", f"Timing schedule saved for Channel {idx+1}.")

    # ------------------------------------------------------------------
    # TAB 4 — CALIBRATION
    # ------------------------------------------------------------------
    def _build_tab_calibration(self):
        tab = self._tab_frame("Calibration")

        fr = tk.Frame(tab, bg=C["bg"])
        fr.pack(fill="both", expand=True, padx=8, pady=8)
        fr.columnconfigure(0, weight=1)
        fr.columnconfigure(1, weight=1)

        for i in range(2):
            outer, body = self._card(fr, f"CHANNEL {i+1}  CALIBRATION")
            outer.grid(row=0, column=i, sticky="nsew", padx=4)
            self._build_calib_panel(body, i)

    def _build_calib_panel(self, parent, idx):
        tk.Label(parent, text="Dispensing", font=self.f_head,
                 bg=C["panel"], fg=C["accent"]).pack(anchor="w", pady=(0, 8))

        grid = tk.Frame(parent, bg=C["panel"])
        grid.pack(fill="x")

        self._cal_vol   = getattr(self, "_cal_vol",  [None, None])
        self._cal_time  = getattr(self, "_cal_time", [None, None])
        self._cal_adj   = getattr(self, "_cal_adj",  [None, None])

        self._cal_vol[idx]  = tk.DoubleVar(value=10.0)
        self._cal_time[idx] = tk.DoubleVar(value=2.0)
        self._cal_adj[idx]  = tk.DoubleVar(value=0.0)

        self._input_box(grid, "Disp. Vol.:", self._cal_vol[idx],  "mL", row=0, col=0)
        self._input_box(grid, "Disp. Time:", self._cal_time[idx], "s",  row=1, col=0)

        sep = tk.Frame(parent, bg=C["border"], height=1)
        sep.pack(fill="x", pady=8)

        btn_row = tk.Frame(parent, bg=C["panel"])
        btn_row.pack(fill="x", pady=4)
        self._big_btn(btn_row, "  START",
                      lambda i=idx: self._run_calibration(i),
                      C["green"]).pack(side="left", padx=(0, 8))
        self._big_btn(btn_row, "  RESET",
                      lambda i=idx: self._reset_calibration(i),
                      C["accent2"]).pack(side="left")

        sep2 = tk.Frame(parent, bg=C["border"], height=1)
        sep2.pack(fill="x", pady=8)

        # Adjust volume
        tk.Label(parent, text="Input Adjust Volume:",
                 font=self.f_label, bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")

        adj_row = tk.Frame(parent, bg=C["panel"])
        adj_row.pack(fill="x", pady=4)

        self._big_btn(adj_row, "ADD",
                      lambda i=idx: self._adj_volume(i, +0.1),
                      C["accent"]).pack(side="left", padx=(0, 6))

        adj_box = tk.Frame(adj_row, bg=C["input_bg"], padx=8, pady=4)
        adj_box.pack(side="left")
        tk.Label(adj_box, textvariable=self._cal_adj[idx],
                 font=("Consolas", 14, "bold"),
                 bg=C["input_bg"], fg="white", width=8).pack(side="left")
        tk.Label(adj_box, text=" mL", font=self.f_small,
                 bg=C["input_bg"], fg="#90CAF9").pack(side="left")

        self._big_btn(adj_row, "DEC",
                      lambda i=idx: self._adj_volume(i, -0.1),
                      C["orange"]).pack(side="left", padx=6)

        # Calibration factor
        sep3 = tk.Frame(parent, bg=C["border"], height=1)
        sep3.pack(fill="x", pady=8)
        tk.Label(parent, text="Calibration Factor:",
                 font=self.f_label, bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")

        cf_row = tk.Frame(parent, bg=C["panel"])
        cf_row.pack(fill="x", pady=4)
        tk.Entry(cf_row, textvariable=self._calib_factor[idx],
                 font=("Consolas", 13), bg=C["input_bg"], fg="white",
                 insertbackground="white", bd=0, width=10).pack(side="left", padx=4)
        self._big_btn(cf_row, "Apply",
                      lambda i=idx: self._apply_calib_factor(i),
                      C["cyan"]).pack(side="left", padx=6)

        # Calculated flow info
        info_fr = tk.Frame(parent, bg=C["row_alt"], padx=10, pady=8)
        info_fr.pack(fill="x", pady=8)
        self._calib_info = getattr(self, "_calib_info", [None, None])
        self._calib_info[idx] = tk.Label(info_fr, text="Run calibration to calculate actual flow rate",
                                          font=self.f_small, bg=C["row_alt"], fg=C["text_dim"],
                                          wraplength=280, justify="left")
        self._calib_info[idx].pack()

    def _run_calibration(self, idx):
        pump = self.pump1 if idx == 0 else self.pump2
        if not pump or not pump.is_connected():
            messagebox.showwarning("Not Connected", f"Connect Channel {idx+1} first.")
            return
        vol  = self._cal_vol[idx].get()
        rpm  = self._rpm_var[idx].get()
        tube = self._tube_var[idx].get()
        t    = calc_run_time(tube, rpm, vol) * self._calib_factor[idx].get()
        self._calib_info[idx].config(
            text=f"Running {vol:.2f} mL @ {rpm:.1f} RPM\nEstimated time: {t:.2f} s\n"
                 f"Flow rate: {calc_flow_rate(tube, rpm):.3f} mL/min")

        def run():
            pump.set_speed(rpm)
            pump.set_direction(True)
            pump.start()
            time.sleep(t)
            pump.stop()
        threading.Thread(target=run, daemon=True).start()

    def _reset_calibration(self, idx):
        self._cal_adj[idx].set(0.0)
        self._calib_factor[idx].set(1.0)

    def _adj_volume(self, idx, delta):
        v = round(self._cal_adj[idx].get() + delta, 2)
        self._cal_adj[idx].set(v)

    def _apply_calib_factor(self, idx):
        f = self._calib_factor[idx].get()
        self._settings[f"calib{idx+1}"] = f
        save_settings(self._settings)
        messagebox.showinfo("Calibration", f"Channel {idx+1} calibration factor set to {f:.4f}")

    # ------------------------------------------------------------------
    # TAB 5 — RECIPES (Common Mode)
    # ------------------------------------------------------------------
    def _build_tab_common_mode(self):
        tab = self._tab_frame("Common Mode")
        outer, body = self._card(tab, "COMMON MODE  —  Recipe Programs")
        outer.pack(fill="both", expand=True, padx=8, pady=8)

        # Treeview
        cols = ("no","channel","tube","vol","time","pause","repeat","speed","suckback")
        self._recipe_tree = ttk.Treeview(body, columns=cols, show="headings", height=12)

        headers = {"no":"#","channel":"Channel","tube":"Tube",
                   "vol":"Vol(mL)","time":"Time(s)","pause":"Pause(s)",
                   "repeat":"Repeat","speed":"RPM","suckback":"Suck-Back"}
        widths  = {"no":35,"channel":75,"tube":75,"vol":75,
                   "time":70,"pause":70,"repeat":60,"speed":70,"suckback":80}
        for col in cols:
            self._recipe_tree.heading(col, text=headers[col])
            self._recipe_tree.column(col, width=widths[col], anchor="center")

        self._recipe_tree.pack(fill="both", expand=True)
        self._recipe_tree.tag_configure("evenrow", background=C["row_alt"])

        # Load saved recipes
        self._recipes = self._settings.get("recipes", [])
        self._refresh_recipe_tree()

        # Buttons
        btn_row = tk.Frame(body, bg=C["panel"])
        btn_row.pack(fill="x", pady=6)

        self._big_btn(btn_row, "  ADD",    self._add_recipe,    C["green"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  DELETE", self._del_recipe,    C["red"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  CLEAR",  self._clear_recipes, C["orange"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  RUN ALL",self._run_recipes,   C["accent"]).pack(side="right")

    def _refresh_recipe_tree(self):
        for row in self._recipe_tree.get_children():
            self._recipe_tree.delete(row)
        for i, r in enumerate(self._recipes):
            tag = "evenrow" if i % 2 == 0 else ""
            self._recipe_tree.insert("", "end", values=(
                i+1,
                f"Pump {r['channel']}",
                r.get("tube","2x1mm"),
                f"{r['vol']:.2f}",
                f"{r['time']:.2f}",
                f"{r['pause']:.2f}",
                r.get("repeat",1),
                f"{r['speed']:.1f}",
                f"{r.get('suckback',0.0):.1f} deg",
            ), tags=(tag,))

    def _add_recipe(self):
        dlg = tk.Toplevel(self)
        dlg.title("Add Program — Common Mode")
        dlg.geometry("420x480")
        dlg.configure(bg=C["panel"])
        dlg.resizable(False, False)
        dlg.grab_set()

        # Title
        hdr = tk.Frame(dlg, bg=C["accent"], pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="  ADD PROGRAM", font=self.f_bold,
                 bg=C["accent"], fg="white").pack(side="left", padx=10)

        body = tk.Frame(dlg, bg=C["panel"], padx=16, pady=10)
        body.pack(fill="both", expand=True)

        # Channel selection — big buttons
        tk.Label(body, text="Select Channel:", font=self.f_bold,
                 bg=C["panel"], fg=C["text"]).grid(row=0, column=0,
                 columnspan=4, sticky="w", pady=(0,6))

        ch_var = tk.IntVar(value=1)
        for ch, col in [(1, 1), (2, 3)]:
            rb = tk.Radiobutton(body, text=f"  PUMP {ch}  ",
                                variable=ch_var, value=ch,
                                font=("Segoe UI", 12, "bold"),
                                bg=C["accent"], fg="white",
                                selectcolor=C["green"],
                                activebackground=C["accent"],
                                relief="flat", padx=16, pady=8,
                                indicatoron=False)
            rb.grid(row=0, column=col, padx=6, pady=4)

        sep = tk.Frame(body, bg=C["border"], height=1)
        sep.grid(row=1, column=0, columnspan=4, sticky="ew", pady=8)

        # Fields
        fields = {}
        rows = [
            ("tube",   "Tube Size:",      "2x1mm"),
            ("vol",    "Disp. Vol. (mL):","10.0"),
            ("time",   "Disp. Time (s):", "2.0"),
            ("pause",  "Pause Time (s):", "1.0"),
            ("repeat", "Repeat:",         "1"),
            ("speed",  "Speed (RPM):",    "60.0"),
            ("suckback","Suck-Back (deg):","0.0"),
        ]

        for i, (k, lbl, default) in enumerate(rows):
            tk.Label(body, text=lbl, font=self.f_label,
                     bg=C["panel"], fg=C["text_dim"],
                     width=18, anchor="w").grid(
                         row=i+2, column=0, columnspan=2,
                         sticky="w", pady=4, padx=(0,8))
            var = tk.StringVar(value=default)
            fields[k] = var

            if k == "tube":
                cb = ttk.Combobox(body, textvariable=var,
                                  values=list(TUBE_DATA.keys()),
                                  width=14, state="readonly")
                cb.grid(row=i+2, column=2, columnspan=2,
                        sticky="w", pady=4)
            else:
                ebox = tk.Frame(body, bg=C["input_bg"], padx=4, pady=3)
                ebox.grid(row=i+2, column=2, columnspan=2,
                          sticky="w", pady=4)
                tk.Entry(ebox, textvariable=var,
                         font=("Consolas", 12, "bold"),
                         bg=C["input_bg"], fg="white",
                         insertbackground="white", bd=0,
                         width=12).pack()

        # Calc info
        info_lbl = tk.Label(body, text="", font=self.f_small,
                            bg=C["row_alt"], fg=C["accent"],
                            wraplength=360, justify="left", pady=4)
        info_lbl.grid(row=len(rows)+2, column=0, columnspan=4,
                      sticky="ew", pady=4)

        def update_calc(*a):
            try:
                vol   = float(fields["vol"].get())
                speed = float(fields["speed"].get())
                tube  = fields["tube"].get()
                t     = calc_run_time(tube, speed, vol)
                flow  = calc_flow_rate(tube, speed)
                info_lbl.config(
                    text=f"Flow: {flow:.3f} mL/min  |  Run time: {t:.2f} s  |  Tube: {tube}",
                    bg=C["row_alt"])
            except: pass

        for k in ["vol", "speed"]:
            fields[k].trace_add("write", update_calc)

        def save():
            try:
                rec = {
                    "channel":  ch_var.get(),
                    "tube":     fields["tube"].get(),
                    "vol":      float(fields["vol"].get()),
                    "time":     float(fields["time"].get()),
                    "pause":    float(fields["pause"].get()),
                    "repeat":   int(fields["repeat"].get()),
                    "speed":    float(fields["speed"].get()),
                    "suckback": float(fields["suckback"].get()),
                }
                self._recipes.append(rec)
                self._settings["recipes"] = self._recipes
                save_settings(self._settings)
                self._refresh_recipe_tree()
                dlg.destroy()
            except ValueError as e:
                messagebox.showerror("Invalid Input", str(e))

        btn_row = tk.Frame(body, bg=C["panel"])
        btn_row.grid(row=len(rows)+3, column=0, columnspan=4, pady=10)
        self._big_btn(btn_row, "  SAVE PROGRAM", save, C["green"]).pack(side="left", padx=8)
        self._big_btn(btn_row, "  CANCEL", dlg.destroy, C["red"]).pack(side="left")

    def _del_recipe(self):
        sel = self._recipe_tree.selection()
        if not sel:
            return
        idx = self._recipe_tree.index(sel[0])
        self._recipes.pop(idx)
        self._settings["recipes"] = self._recipes
        save_settings(self._settings)
        self._refresh_recipe_tree()

    def _clear_recipes(self):
        if messagebox.askyesno("Clear", "Clear all recipes?"):
            self._recipes.clear()
            self._settings["recipes"] = self._recipes
            save_settings(self._settings)
            self._refresh_recipe_tree()

    def _run_recipes(self):
        if not self._recipes:
            messagebox.showwarning("Empty", "No recipes to run.")
            return
        def run_all():
            for r in self._recipes:
                pump = self.pump1 if r["channel"] == 1 else self.pump2
                if not pump or not pump.is_connected():
                    continue
                tube   = r["tube"]
                speed  = r["speed"]
                vol    = r["vol"]
                pause  = r["pause"]
                repeat = r["repeat"]
                run_t  = calc_run_time(tube, speed, vol)
                for _ in range(repeat):
                    pump.set_speed(speed)
                    pump.set_direction(True)
                    pump.start()
                    time.sleep(run_t)
                    pump.stop()
                    time.sleep(pause)
        threading.Thread(target=run_all, daemon=True).start()

    # ------------------------------------------------------------------
    # TAB 6 — SETTINGS
    # ------------------------------------------------------------------
    def _build_tab_settings(self):
        tab = self._tab_frame("Settings")

        fr = tk.Frame(tab, bg=C["bg"])
        fr.pack(fill="both", expand=True, padx=8, pady=8)
        fr.columnconfigure(0, weight=1)
        fr.columnconfigure(1, weight=1)

        # Communication card
        outer, body = self._card(fr, "COMMUNICATIONS")
        outer.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        tk.Label(body, text="COM Port:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")
        tk.Entry(body, textvariable=self._port_var, font=self.f_mono,
                 bg=C["input_bg"], fg="white",
                 insertbackground="white", width=14).pack(anchor="w", pady=4)

        tk.Label(body, text="Baud Rate: 9600  |  Parity: Even  |  Stop: 1",
                 font=self.f_small, bg=C["panel"], fg=C["text_dim"]).pack(anchor="w", pady=2)

        # Channel 1
        tk.Label(body, text="\nChannel 1 Settings:",
                 font=self.f_bold, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        tk.Label(body, text="Slave ID:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")
        tk.Entry(body, textvariable=self._slave_var[0], font=self.f_mono,
                 bg=C["input_bg"], fg="white",
                 insertbackground="white", width=6).pack(anchor="w", pady=2)
        tk.Label(body, text="Tube Size:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")
        cb1 = ttk.Combobox(body, textvariable=self._tube_var[0],
                     values=list(TUBE_DATA.keys()), width=12,
                     state="readonly")
        cb1.pack(anchor="w", pady=2)
        cb1.bind("<<ComboboxSelected>>", lambda e: self._on_tube_change(0))

        # Tube info display ch1
        self._tube_info_lbl = [None, None]
        self._tube_info_lbl[0] = tk.Label(body, text="",
                                           font=self.f_small,
                                           bg=C["panel"], fg=C["accent"])
        self._tube_info_lbl[0].pack(anchor="w")

        # Channel 2
        tk.Label(body, text="\nChannel 2 Settings:",
                 font=self.f_bold, bg=C["panel"], fg=C["text"]).pack(anchor="w")
        tk.Label(body, text="Slave ID:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")
        tk.Entry(body, textvariable=self._slave_var[1], font=self.f_mono,
                 bg=C["input_bg"], fg="white",
                 insertbackground="white", width=6).pack(anchor="w", pady=2)
        tk.Label(body, text="Tube Size:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")
        cb2 = ttk.Combobox(body, textvariable=self._tube_var[1],
                     values=list(TUBE_DATA.keys()), width=12,
                     state="readonly")
        cb2.pack(anchor="w", pady=2)
        cb2.bind("<<ComboboxSelected>>", lambda e: self._on_tube_change(1))

        self._tube_info_lbl[1] = tk.Label(body, text="",
                                           font=self.f_small,
                                           bg=C["panel"], fg=C["accent"])
        self._tube_info_lbl[1].pack(anchor="w")

        self._big_btn(body, "  CONNECT BOTH",
                      self._connect_all, C["green"]).pack(fill="x", pady=8)
        self._big_btn(body, "  DISCONNECT",
                      self._disconnect_all, C["red"]).pack(fill="x", pady=4)
        self._big_btn(body, "  TEST PORT ONLY",
                      self._test_port, C["teal"]).pack(fill="x", pady=4)

        self._test_result = tk.Label(body, text="",
                                      font=self.f_small,
                                      bg=C["panel"], fg=C["text_dim"],
                                      wraplength=250, justify="left")
        self._test_result.pack(pady=4)

        # Tube reference card
        outer2, body2 = self._card(fr, "TUBE FLOW RATE REFERENCE")
        outer2.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)

        cols = ("tube", "max_flow", "at_60", "at_100", "at_200")
        tree = ttk.Treeview(body2, columns=cols, show="headings", height=8)
        for col, label, w in [
            ("tube",     "Tube Size",     90),
            ("max_flow", "Max (mL/min)", 110),
            ("at_60",    "@ 60 RPM",      90),
            ("at_100",   "@ 100 RPM",     90),
            ("at_200",   "@ 200 RPM",     90),
        ]:
            tree.heading(col, text=label)
            tree.column(col,  width=w, anchor="center")

        for i, (key, data) in enumerate(TUBE_DATA.items()):
            mf = data["max_flow"]
            tree.insert("", "end", values=(
                data["label"],
                f"{mf:.2f}",
                f"{calc_flow_rate(key, 60):.3f}",
                f"{calc_flow_rate(key, 100):.3f}",
                f"{calc_flow_rate(key, 200):.3f}",
            ), tags=("even" if i%2==0 else "odd",))
        tree.tag_configure("even", background=C["row_alt"])
        tree.pack(fill="both", expand=True)

        tk.Label(body2,
                 text="Note: Actual flow may vary. Use calibration to fine-tune.",
                 font=self.f_small, bg=C["panel"], fg=C["text_dim"],
                 wraplength=300).pack(pady=6)

        # Save settings button
        self._big_btn(body2, "  SAVE SETTINGS",
                      self._save_all_settings, C["accent"]).pack(fill="x", pady=4)

        # ── Suck-Back Settings ───────────────────────────────────────
        outer3, body3 = self._card(fr, "SUCK-BACK ANGLE (Anti-Drip)")
        outer3.grid(row=1, column=0, columnspan=2, sticky="ew",
                    padx=4, pady=4)

        sb_info = tk.Frame(body3, bg=C["row_alt"], padx=10, pady=8)
        sb_info.pack(fill="x", pady=(0,8))
        tk.Label(sb_info,
                 text=("Suck-back angle range: 0 - 360 deg\n"
                       "When transferring viscous liquid, setting the suck-back "
                       "angle can prevent liquid dripping when the pump stops."),
                 font=self.f_small, bg=C["row_alt"], fg=C["orange"],
                 wraplength=700, justify="left").pack(anchor="w")

        sb_row = tk.Frame(body3, bg=C["panel"])
        sb_row.pack(fill="x")

        for i in range(2):
            ch_fr = tk.Frame(sb_row, bg=C["panel"], padx=20)
            ch_fr.pack(side="left", fill="x", expand=True)

            tk.Label(ch_fr, text=f"Pump {i+1} Suck-Back Angle:",
                     font=self.f_bold, bg=C["panel"],
                     fg=C["accent"]).pack(anchor="w", pady=(0,4))

            sl_fr = tk.Frame(ch_fr, bg=C["panel"])
            sl_fr.pack(fill="x")

            sl = tk.Scale(sl_fr, from_=0, to=360, orient="horizontal",
                          variable=self._suckback_var[i], resolution=0.5,
                          bg=C["panel"], fg=C["text"],
                          troughcolor=C["bg"], highlightthickness=0,
                          length=200)
            sl.pack(side="left", fill="x", expand=True)

            val_fr = tk.Frame(sl_fr, bg=C["input_bg"], padx=8, pady=4)
            val_fr.pack(side="left", padx=6)
            tk.Label(val_fr, textvariable=self._suckback_var[i],
                     font=("Consolas", 13, "bold"),
                     bg=C["input_bg"], fg="white",
                     width=6).pack(side="left")
            tk.Label(val_fr, text=" deg",
                     font=self.f_small,
                     bg=C["input_bg"], fg="#90CAF9").pack(side="left")

            # Preset buttons
            pre_fr = tk.Frame(ch_fr, bg=C["panel"])
            pre_fr.pack(anchor="w", pady=4)
            tk.Label(pre_fr, text="Quick:", font=self.f_small,
                     bg=C["panel"], fg=C["text_dim"]).pack(side="left")
            for ang in [0, 45, 90, 180, 270, 360]:
                tk.Button(pre_fr, text=str(ang),
                          command=lambda a=ang, idx=i: self._suckback_var[idx].set(a),
                          font=("Segoe UI", 9), bg=C["bg"], fg=C["text"],
                          relief="flat", padx=6, pady=2,
                          cursor="hand2").pack(side="left", padx=1)

    # ------------------------------------------------------------------
    # Connection Logic
    # ------------------------------------------------------------------
    def _connect_all(self):
        port = self._port_var.get().strip()
        self._conn_lbl.config(text="● CONNECTING...", fg="#FFD740")

        def connect():
            # Reset any old connection first
            SharedModbusClient.reset()
            time.sleep(0.2)

            # ONE shared connection for both pumps
            shared = SharedModbusClient.get()
            ok = shared.connect(port=port)

            if ok:
                self.pump1 = PumpDriver(slave_id=self._slave_var[0].get())
                self.pump2 = PumpDriver(slave_id=self._slave_var[1].get())
                self.pump1.connect()
                self.pump2.connect()
                # Start watchdog
                self._start_watchdog()
            else:
                self.pump1 = None
                self.pump2 = None

            def update_ui():
                if ok:
                    self._conn_lbl.config(text="● CONNECTED", fg="#69F0AE")
                    self._update_tube_labels()
                else:
                    self._conn_lbl.config(text="● DISCONNECTED", fg="#FF8A80")
                    messagebox.showerror("Connection Failed",
                        f"Could not open {port}\n\n"
                        "Check:\n"
                        "1. COM port is correct (check Device Manager)\n"
                        "2. USB-RS485 adapter is plugged in\n"
                        "3. Close any other program using this port\n"
                        "4. Try unplugging and replugging USB adapter")
            self.after(0, update_ui)

        threading.Thread(target=connect, daemon=True).start()

    def _start_watchdog(self):
        """Monitor connection — detect cable removal and stop motors."""
        def watch():
            while True:
                time.sleep(2.0)
                shared = SharedModbusClient.get()
                if not shared.is_connected():
                    # Cable removed — stop everything
                    for pump in [self.pump1, self.pump2]:
                        if pump:
                            pump._running = False
                    self.after(0, self._on_cable_removed)
                    break
        threading.Thread(target=watch, daemon=True).start()

    def _on_cable_removed(self):
        self._conn_lbl.config(text="● CABLE REMOVED", fg="#FF5252")
        for idx in range(2):
            self._stop_dispense(idx)
            if hasattr(self, "_status_dot") and self._status_dot[idx]:
                self._status_dot[idx].config(fg=C["red"])
            if hasattr(self, "_status_txt") and self._status_txt[idx]:
                self._status_txt[idx].config(text="STOPPED", fg=C["red"])

    def _disconnect_all(self):
        for idx in range(2):
            self._stop_dispense(idx)
        for pump in [self.pump1, self.pump2]:
            if pump:
                try:
                    pump.stop()
                    pump.disconnect()
                except: pass
        self.pump1 = None
        self.pump2 = None
        SharedModbusClient.reset()
        self._conn_lbl.config(text="● DISCONNECTED", fg="#FF8A80")

    def _on_disp_speed_change(self, idx):
        """Update calculated time when dispensing speed slider moves."""
        self._update_calc_time(idx)

    def _set_disp_speed(self, idx, rpm):
        """Set dispensing speed from preset button."""
        self._dv_speed[idx].set(rpm)
        self._update_calc_time(idx)

    def _update_calc_time(self, idx):
        """Show calculated run time based on volume + speed + tube."""
        try:
            vol   = self._dv_vol[idx].get()
            speed = self._dv_speed[idx].get()
            tube  = self._tube_var[idx].get()
            calib = self._calib_factor[idx].get()
            if speed <= 0:
                return
            run_t  = calc_run_time(tube, speed, vol) * calib
            flow   = calc_flow_rate(tube, speed)
            msg    = (f"Volume: {vol:.2f} mL  |  "
                      f"Speed: {speed:.1f} RPM  |  "
                      f"Flow: {flow:.3f} mL/min  |  "
                      f"Run time: {run_t:.2f} s")
            if hasattr(self, "_calc_time_lbl") and self._calc_time_lbl[idx]:
                self._calc_time_lbl[idx].config(text=msg, fg=C["accent"])
        except Exception:
            pass

    def _update_tube_labels(self):
        for i in range(2):
            tube = self._tube_var[i].get()
            data = TUBE_DATA.get(tube, {})
            rpm  = self._rpm_var[i].get() if hasattr(self, "_rpm_var") and self._rpm_var[i] else 0
            flow = calc_flow_rate(tube, rpm)
            # Update tube label
            if hasattr(self, "_tube_lbl") and self._tube_lbl[i]:
                self._tube_lbl[i].config(text=data.get("label", tube))
            # Update flow rate label
            if hasattr(self, "_flow_lbl") and self._flow_lbl[i]:
                self._flow_lbl[i].config(
                    text=f"{flow:.3f} mL/min" if rpm > 0 else f"Max: {data.get('max_flow',0):.2f} mL/min")
            # Update RPM label
            if hasattr(self, "_rpm_lbl") and self._rpm_lbl[i]:
                self._rpm_lbl[i].config(text=f"{rpm:.1f} RPM")

    def _on_tube_change(self, idx):
        """Called when tube size dropdown changes in settings."""
        tube = self._tube_var[idx].get()
        data = TUBE_DATA.get(tube, {})
        self._update_tube_labels()
        # Show tube info in settings panel
        if hasattr(self, "_tube_info_lbl") and self._tube_info_lbl[idx]:
            self._tube_info_lbl[idx].config(
                text=f"Max: {data.get('max_flow',0):.2f} mL/min @ 350 RPM")
        # Save immediately
        self._settings[f"tube{idx+1}"] = tube
        save_settings(self._settings)

    # ------------------------------------------------------------------
    # Motor Control Commands
    # ------------------------------------------------------------------
    def _get_pump(self, idx):
        return self.pump1 if idx == 0 else self.pump2

    def _cmd_start(self, idx):
        pump = self._get_pump(idx)
        if not pump or not pump.is_connected():
            messagebox.showwarning("Not Connected",
                                   f"Connect Channel {idx+1} in Settings first.")
            return
        def run():
            rpm  = self._rpm_var[idx].get()
            fwd  = self._dir_var[idx].get() == "CW"
            # Send speed first, wait, then start
            pump.set_speed(rpm)
            time.sleep(0.1)
            pump.set_direction(fwd)
            time.sleep(0.1)
            pump.start()
            self.after(0, lambda: self._update_motor_ui(idx))
        threading.Thread(target=run, daemon=True).start()

    def _cmd_stop(self, idx):
        pump = self._get_pump(idx)
        def force_stop():
            if pump:
                # Try stop 3 times no matter what
                for _ in range(3):
                    try:
                        SharedModbusClient.get().write_register(1000, 0, pump.slave_id)
                    except: pass
                    time.sleep(0.05)
                pump._running = False
            self.after(0, lambda: self._update_motor_ui(idx))
        threading.Thread(target=force_stop, daemon=True).start()

    def _set_rpm(self, idx, rpm):
        self._rpm_var[idx].set(rpm)
        pump = self._get_pump(idx)
        if pump and pump.is_connected():
            def send(r=rpm, p=pump):
                try: p.set_speed(r)
                except: pass
            threading.Thread(target=send, daemon=True).start()
        # Update displays immediately
        tube = self._tube_var[idx].get()
        flow = calc_flow_rate(tube, rpm)
        if hasattr(self, "_flow_lbl") and self._flow_lbl[idx]:
            self._flow_lbl[idx].config(text=f"{flow:.3f} mL/min")
        if hasattr(self, "_rpm_lbl") and self._rpm_lbl[idx]:
            self._rpm_lbl[idx].config(text=f"{rpm:.1f} RPM")
        self._rpm_display_lbl = getattr(self, "_rpm_display_lbl", [None,None])

    def _on_rpm_change(self, idx):
        pump = self._get_pump(idx)
        rpm  = self._rpm_var[idx].get()
        # Always send speed if connected — whether running or not
        if pump and pump.is_connected():
            def send_rpm(r=rpm, p=pump):
                try:
                    p.set_speed(r)
                except: pass
            threading.Thread(target=send_rpm, daemon=True).start()
        # Always update display
        tube = self._tube_var[idx].get()
        flow = calc_flow_rate(tube, rpm)
        if hasattr(self, "_flow_lbl") and self._flow_lbl[idx]:
            self._flow_lbl[idx].config(text=f"{flow:.3f} mL/min")
        if hasattr(self, "_rpm_lbl") and self._rpm_lbl[idx]:
            self._rpm_lbl[idx].config(text=f"{rpm:.1f} RPM")

    def _set_direction(self, idx):
        pump = self._get_pump(idx)
        if pump and pump.is_connected():
            fwd = self._dir_var[idx].get() == "CW"
            threading.Thread(target=lambda: pump.set_direction(fwd),
                             daemon=True).start()
        d = self._dir_var[idx].get()
        if hasattr(self, "_dir_lbl") and self._dir_lbl[idx]:
            self._dir_lbl[idx].config(text=d,
                bg=C["green"] if d == "CW" else C["orange"])

    def _reset_volume(self, idx):
        self._disp_volume[idx].set(0.0)
        self._total_vol[idx] = 0.0

    def _update_motor_ui(self, idx):
        pump = self._get_pump(idx)
        if not pump:
            return
        running = pump.is_running
        if hasattr(self, "_status_dot") and self._status_dot[idx]:
            color = C["green"] if running else C["red"]
            self._status_dot[idx].config(fg=color)
        if hasattr(self, "_status_txt") and self._status_txt[idx]:
            txt = "RUNNING" if running else "STOPPED"
            color = C["green"] if running else C["red"]
            self._status_txt[idx].config(text=txt, fg=color)

    # ------------------------------------------------------------------
    # Dispensing Logic
    # ------------------------------------------------------------------
    def _run_dispense(self, idx):
        pump = self._get_pump(idx)
        if not pump or not pump.is_connected():
            messagebox.showwarning("Not Connected",
                                   f"Connect Channel {idx+1} in Settings first.")
            return

        # DOUBLE-START PROTECTION — ignore if already running
        existing = self._stop_events.get(idx)
        if existing and not existing.is_set():
            # Already running — do nothing, protect the running cycle
            return

        # Stop any existing dispense cleanly
        self._stop_dispense(idx)
        time.sleep(0.1)

        vol    = self._dv_vol[idx].get()
        pause  = self._dv_pause[idx].get()
        repeat = self._dv_rep[idx].get()
        speed  = self._dv_speed[idx].get()
        tube   = self._tube_var[idx].get()
        calib  = self._calib_factor[idx].get()
        run_t  = calc_run_time(tube, speed, vol) * calib

        stop_ev = threading.Event()
        self._stop_events[idx] = stop_ev

        def run():
            for cycle in range(repeat):
                if stop_ev.is_set():
                    break
                # Always get LATEST tube setting before each cycle
                current_tube  = self._tube_var[idx].get()
                current_run_t = calc_run_time(current_tube, speed, vol) * calib
                self.after(0, lambda c=cycle, t=current_tube: (
                    self._disp_counter[idx].config(text=f"{c+1} / {repeat}"),
                    self._disp_status[idx].config(
                        text=f"Dispensing {vol:.2f} mL  [{t}]...")
                ))
                pump.set_speed(speed)
                pump.set_direction(True)
                pump.start()
                self.after(0, lambda: self._update_motor_ui(idx))
                run_t = current_run_t

                start = time.time()
                while True:
                    if stop_ev.is_set():
                        break
                    elapsed = time.time() - start
                    if elapsed >= run_t:
                        break
                    pct       = min((elapsed / run_t) * 100, 100)
                    dispensed = round(calc_volume(tube, speed, elapsed) * calib, 3)
                    total_now = round(self._total_vol[idx] + dispensed, 3)
                    def _upd(p=pct, d=total_now, i=idx):
                        try:
                            self._disp_prog[i].configure(value=p)
                            self._disp_volume[i].set(d)
                            self._disp_status[i].config(
                                text=f"Dispensing... {d:.3f} mL")
                        except: pass
                    self.after(0, _upd)
                    time.sleep(0.1)

                pump.stop()
                # Apply suck-back after stop (reverse briefly)
                sb_angle = self._suckback_var[idx].get()
                if sb_angle > 0:
                    sb_time = (sb_angle / 360.0) / (speed / 60.0)
                    pump.set_direction(False)  # reverse
                    pump.start()
                    time.sleep(sb_time)
                    pump.stop()
                    pump.set_direction(True)   # back to forward
                self.after(0, lambda: self._update_motor_ui(idx))
                # Update total volume
                self._total_vol[idx] += vol
                self.after(0, lambda v=self._total_vol[idx]: (
                    self._disp_volume[idx].set(round(v, 3))
                ))

                if cycle < repeat - 1 and not stop_ev.is_set():
                    self.after(0, lambda: self._disp_status[idx].config(
                        text=f"Pausing {pause:.1f} s..."))
                    time.sleep(pause)

            self.after(0, lambda: (
                self._disp_prog[idx].configure(value=0),
                self._disp_status[idx].config(text="Complete!" if not stop_ev.is_set() else "Stopped"),
                self._disp_counter[idx].config(text=f"{repeat} / {repeat}" if not stop_ev.is_set() else "--")
            ))

        t = threading.Thread(target=run, daemon=True)
        self._threads[idx] = t
        t.start()

    def _stop_dispense(self, idx):
        ev = self._stop_events.get(idx)
        if ev:
            ev.set()
        pump = self._get_pump(idx)
        if pump and pump.is_connected():
            threading.Thread(target=pump.stop, daemon=True).start()
        self.after(0, lambda: self._update_motor_ui(idx))

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------
    def _start_clock(self):
        def tick():
            self._clock_lbl.config(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
            self.after(1000, tick)
        tick()

    # ------------------------------------------------------------------
    # Save Settings
    # ------------------------------------------------------------------
    def _test_port(self):
        """Test if COM port can be opened — shows result in settings panel."""
        port = self._port_var.get().strip()
        self._test_result.config(text=f"Testing {port}...", fg=C["text_dim"])

        def test():
            import io, sys
            try:
                import serial
                old_stderr = sys.stderr
                sys.stderr = io.StringIO()
                try:
                    s = serial.Serial(port=port, baudrate=9600,
                                      parity='E', stopbits=1,
                                      bytesize=8, timeout=1)
                    s.close()
                    result = "OK"
                finally:
                    sys.stderr = old_stderr
            except Exception as e:
                result = f"FAIL: {e}"

            def show():
                if result == "OK":
                    msg = "Port " + port + " opened OK! Now click CONNECT BOTH"
                    self._test_result.config(text=msg, fg=C["green"])
                else:
                    msg = ("Port test failed: " + result +
                           "\n\nTry:\n- Unplug/replug USB adapter\n"
                           "- Close other programs\n"
                           "- Check Device Manager for correct COM number")
                    self._test_result.config(text=msg, fg=C["red"])
            self.after(0, show)

        threading.Thread(target=test, daemon=True).start()

    def _save_all_settings(self):
        self._settings.update({
            "port":      self._port_var.get(),
            "slave1":    self._slave_var[0].get(),
            "slave2":    self._slave_var[1].get(),
            "tube1":     self._tube_var[0].get(),
            "tube2":     self._tube_var[1].get(),
            "calib1":    self._calib_factor[0].get(),
            "calib2":    self._calib_factor[1].get(),
            "suckback1": self._suckback_var[0].get(),
            "suckback2": self._suckback_var[1].get(),
        })
        save_settings(self._settings)
        messagebox.showinfo("Saved", "Settings saved successfully!")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------
    def _on_close(self):
        for idx in range(2):
            self._stop_dispense(idx)
        self._disconnect_all()
        self._save_all_settings()
        self.destroy()


# ============================================================================
if __name__ == "__main__":
    # Set DPI awareness for Windows Panel PCs
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = PumpHMI()
    app.mainloop()