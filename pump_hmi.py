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

        try:
            if self._client:
                self._client.close()
        except: pass
        self._client    = None
        self._connected = False
        self._port      = port

        for attempt in range(3):
            try:
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
                self._connected = False
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
                self._connected = False
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
        self._running = True
        return ok

    def stop(self):
        for _ in range(3):
            try:
                self._write_reg(1000, 0)
            except: pass
        self._running = False
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
    max_flow = TUBE_DATA[tube_key]["max_flow"]
    return (max_flow / MAX_RPM) * rpm

def calc_run_time(tube_key, rpm, volume_ml):
    flow = calc_flow_rate(tube_key, rpm)
    if flow <= 0:
        return 0
    return (volume_ml / flow) * 60.0

def calc_volume(tube_key, rpm, seconds):
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

        self.pump1 = None
        self.pump2 = None
        self._settings = load_settings()
        self._stop_events = {}
        self._threads    = {}
        self._disp_volume = [tk.DoubleVar(value=0.0), tk.DoubleVar(value=0.0)]
        self._total_vol   = [0.0, 0.0]

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
        self._dispensing_active = [
            tk.BooleanVar(value=False),
            tk.BooleanVar(value=False),
        ]
        self._global_direction = [
            tk.StringVar(value=self._settings.get("dir1", "CW")),
            tk.StringVar(value=self._settings.get("dir2", "CW")),
        ]
        self._motor_locked_by = [None, None]

        self._build_fonts()
        self._build_ui()
        self._start_clock()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

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

    def _build_ui(self):
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

        status = tk.Frame(tab, bg=C["header2"], height=36)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        tk.Label(status,
                 text="  OEM-STB Series  |  MODBUS RTU RS485  |  Shenchen Precision Pump",
                 font=self.f_small, bg=C["header2"], fg="#B3E5FC").pack(side="left", pady=8)

    def _build_motor_panel(self, parent, idx):
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

        rpm_fr = tk.Frame(parent, bg=C["panel"])
        rpm_fr.pack(fill="x", pady=2)
        tk.Label(rpm_fr, text="Speed:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"], width=10, anchor="w").pack(side="left")
        self._rpm_lbl = getattr(self, "_rpm_lbl", [None, None])
        self._rpm_lbl[idx] = tk.Label(rpm_fr, text="0.00 RPM",
                                       font=("Consolas", 13, "bold"),
                                       bg=C["panel"], fg=C["text"])
        self._rpm_lbl[idx].pack(side="left")

        tube_fr = tk.Frame(parent, bg=C["panel"])
        tube_fr.pack(fill="x", pady=2)
        tk.Label(tube_fr, text="Tube:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"], width=10, anchor="w").pack(side="left")
        self._tube_lbl = getattr(self, "_tube_lbl", [None, None])
        self._tube_lbl[idx] = tk.Label(tube_fr, text="--",
                                        font=self.f_label,
                                        bg=C["panel"], fg=C["text"])
        self._tube_lbl[idx].pack(side="left")

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

        tk.Label(parent, text="Speed Control (RPM):", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")

        self._rpm_var = getattr(self, "_rpm_var", [None, None])
        self._rpm_var[idx] = tk.IntVar(value=60)

        rpm_row = tk.Frame(parent, bg=C["panel"])
        rpm_row.pack(fill="x", pady=2)

        sl = tk.Scale(rpm_row, from_=1, to=350, orient="horizontal",
                      variable=self._rpm_var[idx], resolution=1,
                      bg=C["panel"], fg=C["text"], troughcolor=C["bg"],
                      highlightthickness=0, length=200,
                      command=lambda v, i=idx: self._on_rpm_change(i))
        sl.pack(side="left", fill="x", expand=True)

        rpm_entry = tk.Entry(rpm_row, textvariable=self._rpm_var[idx],
                             font=("Consolas", 11), bg=C["input_bg"],
                             fg="white", insertbackground="white",
                             width=7, bd=0)
        rpm_entry.pack(side="left", padx=6)
        rpm_entry.bind("<Return>",   lambda e, i=idx: self._on_rpm_change(i))
        rpm_entry.bind("<FocusOut>", lambda e, i=idx: self._rpm_entry_changed(i))
        tk.Label(rpm_row, text="RPM", font=self.f_small,
                 bg=C["panel"], fg=C["text_dim"]).pack(side="left")

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

        dir_row = tk.Frame(parent, bg=C["panel"])
        dir_row.pack(fill="x", pady=2)
        tk.Label(dir_row, text="Direction:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(side="left")

        self._dir_var = getattr(self, "_dir_var", [None, None])
        self._dir_var[idx] = tk.StringVar(value="CW")

        tk.Radiobutton(dir_row, text="CW (Reverse)",
                       variable=self._dir_var[idx], value="CW",
                       command=lambda i=idx: self._set_direction(i),
                       bg=C["panel"], fg=C["text"],
                       selectcolor=C["bg"],
                       activebackground=C["panel"]).pack(side="left", padx=8)
        tk.Radiobutton(dir_row, text="CCW (Forward)",
                       variable=self._dir_var[idx], value="CCW",
                       command=lambda i=idx: self._set_direction(i),
                       bg=C["panel"], fg=C["text"],
                       selectcolor=C["bg"],
                       activebackground=C["panel"]).pack(side="left")

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
        top_fr = tk.Frame(parent, bg=C["panel"])
        top_fr.pack(fill="x", pady=(0,6))

        self._disp_mode = getattr(self, "_disp_mode", [None, None])
        self._disp_mode[idx] = tk.StringVar(value="volume")

        tk.Label(top_fr, text="VOLUME DISPENSING",
                 font=("Segoe UI", 12, "bold"),
                 bg=C["accent"], fg="white",
                 padx=12, pady=6).pack(side="left")

        on_off_fr = tk.Frame(top_fr, bg=C["panel"])
        on_off_fr.pack(side="right", padx=8)
        tk.Label(on_off_fr, text="Dispensing:",
                 font=self.f_label, bg=C["panel"],
                 fg=C["text_dim"]).pack(side="left", padx=(0,6))

        self._disp_toggle_btn = getattr(self, "_disp_toggle_btn", [None, None])
        toggle_btn = tk.Button(on_off_fr, text="OFF",
                               font=("Segoe UI", 11, "bold"),
                               bg=C["red"], fg="white",
                               relief="flat", padx=16, pady=6,
                               cursor="hand2")
        toggle_btn.pack(side="left")
        self._disp_toggle_btn[idx] = toggle_btn

        def make_toggle(btn, var, i):
            def toggle():
                new_val = not var.get()
                var.set(new_val)
                if new_val:
                    btn.config(text="ON", bg=C["green"])
                else:
                    btn.config(text="OFF", bg=C["red"])
                    self._stop_dispense(i)
            return toggle
        toggle_btn.config(
            command=make_toggle(toggle_btn,
                                self._dispensing_active[idx], idx))

        mode_fr = tk.Frame(parent, bg=C["panel"])
        mode_fr.pack(fill="x", pady=(0, 8))

        sep = tk.Frame(parent, bg=C["border"], height=1)
        sep.pack(fill="x", pady=6)

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
        self._dv_speed[idx] = tk.IntVar(value=60)

        self._input_box(grid, "Disp. Vol.:", self._dv_vol[idx],  "mL", row=0, col=0)
        self._input_box(grid, "Disp. Time:", self._dv_time[idx], "s",  row=1, col=0)
        self._input_box(grid, "Pause Time:", self._dv_pause[idx],"s",  row=2, col=0)
        self._input_box(grid, "Repeat:",     self._dv_rep[idx],  "",   row=3, col=0)
        self._input_box(grid, "Speed:",      self._dv_speed[idx],"RPM",row=4, col=0)

        sep_sl = tk.Frame(parent, bg=C["border"], height=1)
        sep_sl.pack(fill="x", pady=6)

        spd_info = tk.Frame(parent, bg=C["row_alt"], padx=10, pady=8)
        spd_info.pack(fill="x", pady=4)
        spd_row = tk.Frame(spd_info, bg=C["row_alt"])
        spd_row.pack(fill="x")
        tk.Label(spd_row, text="Speed (from Dashboard):",
                 font=self.f_label, bg=C["row_alt"],
                 fg=C["text_dim"]).pack(side="left")

        spd_display_fr = tk.Frame(spd_row, bg=C["input_bg"], padx=10, pady=4)
        spd_display_fr.pack(side="left", padx=8)
        self._disp_rpm_display = getattr(self, "_disp_rpm_display", [None, None])
        self._disp_rpm_display[idx] = tk.Label(spd_display_fr,
                                                text="60",
                                                font=("Consolas", 14, "bold"),
                                                bg=C["input_bg"], fg="#64FFDA")
        self._disp_rpm_display[idx].pack(side="left")
        tk.Label(spd_display_fr, text=" RPM",
                 font=self.f_small, bg=C["input_bg"],
                 fg="#90CAF9").pack(side="left")
        tk.Label(spd_info,
                 text="Change speed on Dashboard tab to adjust dispensing speed",
                 font=("Segoe UI", 8), bg=C["row_alt"],
                 fg=C["text_dim"]).pack(anchor="w")

        calc_fr = tk.Frame(parent, bg=C["panel"], padx=8, pady=4)
        calc_fr.pack(fill="x", pady=2)
        self._calc_time_lbl = getattr(self, "_calc_time_lbl", [None, None])
        self._calc_time_lbl[idx] = tk.Label(calc_fr,
                                             text="Set volume to see run time",
                                             font=self.f_small,
                                             bg=C["panel"], fg=C["accent"])
        self._calc_time_lbl[idx].pack(anchor="w")

        self._dv_vol[idx].trace_add("write",
            lambda *a, i=idx: self._update_calc_time(i))

        sep2 = tk.Frame(parent, bg=C["border"], height=1)
        sep2.pack(fill="x", pady=6)

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

        btn_row = tk.Frame(parent, bg=C["panel"])
        btn_row.pack(fill="x", pady=6)

        self._big_btn(btn_row, "  START",
                      lambda i=idx: self._run_dispense(i),
                      C["green"]).pack(side="left", padx=(0, 6))

        self._big_btn(btn_row, "  STOP",
                      lambda i=idx: self._stop_dispense(i),
                      C["red"]).pack(side="left")

        info_fr = tk.Frame(parent, bg=C["row_alt"], padx=8, pady=6)
        info_fr.pack(fill="x", pady=6)
        tk.Label(info_fr, text="Calculated run time will show here",
                 font=self.f_small, bg=C["row_alt"], fg=C["text_dim"]).pack()

    def _update_disp_mode(self, idx):
        pass

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
            outer, body = self._card(fr, f"PUMP {i+1}  AUTO TIMER")
            outer.grid(row=0, column=i, sticky="nsew", padx=4)
            self._build_timing_panel(body, i)

    def _build_timing_panel(self, parent, idx):
        note = tk.Frame(parent, bg=C["row_alt"], padx=10, pady=8)
        note.pack(fill="x", pady=(0,8))
        tk.Label(note,
                 text="Set a time for the pump to start and stop automatically.",
                 font=self.f_small, bg=C["row_alt"], fg=C["text_dim"]).pack(anchor="w")

        for section, label in [("start", "Start Time"), ("stop", "Stop Time")]:
            key = "_timing_" + section + "_" + str(idx)

            row = tk.Frame(parent, bg=C["panel"], pady=6)
            row.pack(fill="x")

            tk.Label(row, text=label + ":",
                     font=self.f_bold, bg=C["panel"],
                     fg=C["text"], width=12, anchor="w").pack(side="left")

            h_var = tk.IntVar(value=0)
            m_var = tk.IntVar(value=0)
            s_var = tk.IntVar(value=0)
            setattr(self, key + "_h", h_var)
            setattr(self, key + "_m", m_var)
            setattr(self, key + "_s", s_var)

            for var, tip, maxval, sep in [
                (h_var, "HH", 23, ":"),
                (m_var, "MM", 59, ":"),
                (s_var, "SS", 59, ""),
            ]:
                sp = ttk.Spinbox(row, from_=0, to=maxval,
                                 textvariable=var,
                                 font=("Consolas", 14, "bold"),
                                 width=3, justify="center",
                                 wrap=True)
                sp.pack(side="left", padx=1)
                if sep:
                    tk.Label(row, text=sep, font=("Consolas", 14),
                             bg=C["panel"], fg=C["text"]).pack(side="left", padx=1)

            en_var = tk.BooleanVar(value=False)
            setattr(self, key + "_en", en_var)
            en_btn = tk.Button(row, text="OFF",
                               font=self.f_bold,
                               bg=C["red"], fg="white",
                               relief="flat", padx=12, pady=4,
                               cursor="hand2")
            en_btn.pack(side="left", padx=10)

            def _make_tog(b, v):
                def t():
                    v.set(not v.get())
                    b.config(text="ON" if v.get() else "OFF",
                             bg=C["green"] if v.get() else C["red"])
                return t
            en_btn.config(command=_make_tog(en_btn, en_var))

            freq_var = tk.StringVar(value="once")
            setattr(self, key + "_freq", freq_var)
            for val, lbl in [("once", "Once"), ("custom", "Daily")]:
                tk.Radiobutton(row, text=lbl,
                               variable=freq_var, value=val,
                               font=self.f_small,
                               bg=C["panel"], fg=C["text"],
                               selectcolor=C["bg"],
                               activebackground=C["panel"]).pack(side="left", padx=4)

        sep = tk.Frame(parent, bg=C["border"], height=1)
        sep.pack(fill="x", pady=8)

        now_row = tk.Frame(parent, bg=C["panel"])
        now_row.pack(fill="x")
        tk.Label(now_row, text="Current time:",
                 font=self.f_small, bg=C["panel"],
                 fg=C["text_dim"]).pack(side="left")
        now_lbl = tk.Label(now_row, text="",
                           font=("Consolas", 12, "bold"),
                           bg=C["panel"], fg=C["accent"])
        now_lbl.pack(side="left", padx=8)
        def tick():
            now_lbl.config(text=datetime.now().strftime("%H:%M:%S"))
            parent.after(1000, tick)
        tick()

        btn_row = tk.Frame(parent, bg=C["panel"])
        btn_row.pack(fill="x", pady=8)
        self._big_btn(btn_row, "  ACTIVATE TIMER",
                      lambda i=idx: self._apply_timing(i),
                      C["accent"]).pack(side="left", padx=(0,6), fill="x", expand=True)

        cancel_btn = tk.Button(btn_row,
                               text="No active timer",
                               command=lambda i=idx: self._cancel_timing(i),
                               font=self.f_btn, bg=C["border"], fg="white",
                               relief="flat", padx=12, pady=10,
                               state="disabled", cursor="hand2")
        cancel_btn.pack(side="left", fill="x", expand=True)
        setattr(self, "_timing_cancel_btn_" + str(idx), cancel_btn)
        setattr(self, "_timer_stop_" + str(idx), None)

        status_lbl = tk.Label(parent, text="Timer not active",
                              font=self.f_small,
                              bg=C["panel"], fg=C["text_dim"])
        status_lbl.pack(pady=4)
        setattr(self, "_timing_status_" + str(idx), status_lbl)

    def _reset_timer_ui(self, idx):
        sl = getattr(self, "_timing_status_" + str(idx), None)
        if sl: sl.config(text="Timer completed.", fg=C["text_dim"])
        btn = getattr(self, "_timing_cancel_btn_" + str(idx), None)
        if btn: btn.config(state="disabled", bg=C["border"], text="No active timer")

    def _cancel_timing(self, idx):
        key = "_timer_stop_" + str(idx)
        ev = getattr(self, key, None)
        if ev:
            ev.set()
        setattr(self, key, None)
        if self._motor_locked_by[idx] == "timing":
            self._motor_locked_by[idx] = None
        sl = getattr(self, "_timing_status_" + str(idx), None)
        if sl: sl.config(text="Timer cancelled.", fg=C["text_dim"])
        btn = getattr(self, "_timing_cancel_btn_" + str(idx), None)
        if btn: btn.config(state="disabled", bg=C["border"], text="No active timer")

    def _apply_timing(self, idx):
        def safe_get(attr):
            try:
                v = getattr(self, attr).get()
                return int(str(v).lstrip("0") or "0")
            except Exception:
                return 0

        sh = safe_get("_timing_start_" + str(idx) + "_h")
        sm = safe_get("_timing_start_" + str(idx) + "_m")
        ss = safe_get("_timing_start_" + str(idx) + "_s")
        start_en = getattr(self, "_timing_start_" + str(idx) + "_en").get()
        eh = safe_get("_timing_stop_" + str(idx) + "_h")
        em = safe_get("_timing_stop_" + str(idx) + "_m")
        es = safe_get("_timing_stop_" + str(idx) + "_s")
        stop_en = getattr(self, "_timing_stop_" + str(idx) + "_en").get()
        start_freq = getattr(self, "_timing_start_" + str(idx) + "_freq").get()
        stop_freq  = getattr(self, "_timing_stop_"  + str(idx) + "_freq").get()

        if not start_en and not stop_en:
            messagebox.showinfo("Timing", "Both Start and Stop are OFF. Enable at least one.")
            return

        if start_en and (sh==0 and sm==0 and ss==0):
            messagebox.showwarning("Invalid Time",
                "Start time is 00:00:00 (midnight). Please set a valid start time.")
            return
        if stop_en and (eh==0 and em==0 and es==0):
            messagebox.showwarning("Invalid Time",
                "Stop time is 00:00:00 (midnight). Please set a valid stop time.")
            return

        if stop_en and not start_en:
            messagebox.showwarning("Timer Setup",
                "Stop ON but Start is OFF. Pump will stop at set time if running.")
        for h,m,s,name in [(sh,sm,ss,"Start"),(eh,em,es,"Stop")]:
            if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
                messagebox.showerror("Invalid Time",
                    name + " time out of range. HH=0-23, MM=0-59, SS=0-59")
                return

        if start_en and stop_en:
            if (eh*3600+em*60+es) <= (sh*3600+sm*60+ss):
                messagebox.showerror("Invalid Time",
                    "Stop time must be AFTER start time!")
                return

        self._cancel_timing(idx)
        time.sleep(0.1)

        stop_ev = threading.Event()
        setattr(self, "_timer_stop_" + str(idx), stop_ev)
        start_fired = [False]
        stop_fired  = [False]

        def time_matches(h, m, s):
            now = datetime.now()
            return now.hour==h and now.minute==m and now.second==s

        def watch():
            while not stop_ev.is_set():
                if start_en and time_matches(sh, sm, ss):
                    if not start_fired[0]:
                        start_fired[0] = True
                        pump = self._get_pump(idx)
                        if pump and pump.is_connected():
                            self._motor_locked_by[idx] = "timing"
                            rpm = self._rpm_var[idx].get()
                            fwd = (self._global_direction[idx].get() == "CW")
                            pump.set_speed(rpm)
                            pump.set_direction(fwd)
                            pump.start()
                            self.after(0, lambda: self._update_motor_ui(idx))
                        if start_freq == "once" and not stop_en:
                            stop_ev.set()
                            return
                else:
                    start_fired[0] = False

                if stop_en and time_matches(eh, em, es):
                    if not stop_fired[0]:
                        stop_fired[0] = True
                        pump = self._get_pump(idx)
                        if pump and pump.is_connected():
                            pump.stop()
                        self._motor_locked_by[idx] = None
                        self.after(0, lambda: self._update_motor_ui(idx))
                        if stop_freq == "once":
                            stop_ev.set()
                            self.after(500, lambda i=idx: self._reset_timer_ui(i))
                            return
                else:
                    stop_fired[0] = False
                time.sleep(0.5)

        threading.Thread(target=watch, daemon=True).start()

        parts = []
        if start_en:
            parts.append("START {:02d}:{:02d}:{:02d} ({})".format(sh,sm,ss,start_freq))
        if stop_en:
            parts.append("STOP  {:02d}:{:02d}:{:02d} ({})".format(eh,em,es,stop_freq))

        sl = getattr(self, "_timing_status_" + str(idx), None)
        if sl: sl.config(text="ACTIVE: " + " | ".join(parts), fg=C["green"])
        btn = getattr(self, "_timing_cancel_btn_" + str(idx), None)
        if btn: btn.config(state="normal", bg=C["red"], text="CANCEL TIMER")

        msg = ("Pump " + str(idx+1) + " timer set!\n\n" +
               "\n".join(parts) + "\n\nClick CANCEL TIMER to stop.")
        messagebox.showinfo("Timer Active", msg)

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
        how_fr = tk.Frame(parent, bg=C["row_alt"], padx=10, pady=8)
        how_fr.pack(fill="x", pady=(0,8))
        tk.Label(how_fr,
                 text=("HOW TO CALIBRATE:\n"
                       "1. Set target volume and click START\n"
                       "2. Measure actual liquid in a measuring cup\n"
                       "3. Enter ACTUAL measured volume below\n"
                       "4. Click APPLY - factor calculated automatically"),
                 justify="left").pack(anchor="w")

        self._cal_vol   = getattr(self, "_cal_vol",  [None, None])
        self._cal_time  = getattr(self, "_cal_time", [None, None])
        self._cal_actual= getattr(self, "_cal_actual",[None, None])

        self._cal_vol[idx]    = tk.DoubleVar(value=10.0)
        self._cal_time[idx]   = tk.DoubleVar(value=5.0)
        self._cal_actual[idx] = tk.DoubleVar(value=10.0)

        grid = tk.Frame(parent, bg=C["panel"])
        grid.pack(fill="x", pady=4)
        self._input_box(grid, "Target Vol.:", self._cal_vol[idx], "mL", row=0, col=0)
        self._calib_time_lbl = getattr(self, "_calib_time_lbl", [None, None])
        self._calib_time_lbl[idx] = None

        btn_row = tk.Frame(parent, bg=C["panel"])
        btn_row.pack(fill="x", pady=6)
        self._big_btn(btn_row, "  START CALIBRATION RUN",
                      lambda i=idx: self._run_calibration(i),
                      C["green"]).pack(side="left", padx=(0,8))
        self._big_btn(btn_row, "  RESET",
                      lambda i=idx: self._reset_calibration(i),
                      C["orange"]).pack(side="left")

        sep = tk.Frame(parent, bg=C["border"], height=1)
        sep.pack(fill="x", pady=8)

        tk.Label(parent,
                 text="Step 3: Enter ACTUAL measured volume:",
                 font=self.f_bold, bg=C["panel"], fg=C["text"]).pack(anchor="w")

        actual_row = tk.Frame(parent, bg=C["panel"])
        actual_row.pack(fill="x", pady=6)
        af = tk.Frame(actual_row, bg=C["input_bg"], padx=8, pady=6)
        af.pack(side="left")
        tk.Entry(af, textvariable=self._cal_actual[idx],
                 font=("Consolas", 16, "bold"),
                 bg=C["input_bg"], fg="white",
                 insertbackground="white", bd=0, width=8).pack(side="left")
        tk.Label(af, text=" mL", font=self.f_small,
                 bg=C["input_bg"], fg="#90CAF9").pack(side="left")

        adj_row = tk.Frame(parent, bg=C["panel"])
        adj_row.pack(fill="x", pady=2)
        tk.Label(adj_row, text="Fine adjust:",
                 font=self.f_small, bg=C["panel"],
                 fg=C["text_dim"]).pack(side="left", padx=(0,6))
        for delta, lbl, color in [(-1.0,"-1.0",C["red"]),
                                   (-0.1,"-0.1",C["red"]),
                                   (+0.1,"+0.1",C["green"]),
                                   (+1.0,"+1.0",C["green"])]:
            tk.Button(adj_row, text=lbl,
                      command=lambda d=delta, i=idx: self._adj_actual(i, d),
                      font=("Segoe UI",10,"bold"),
                      bg=color, fg="white", relief="flat",
                      padx=10, pady=4, cursor="hand2").pack(side="left", padx=2)

        self._big_btn(parent, "  APPLY CALIBRATION",
                      lambda i=idx: self._apply_calib_factor(i),
                      C["accent"]).pack(fill="x", pady=8)

        self._calib_info = getattr(self, "_calib_info", [None, None])
        info_fr = tk.Frame(parent, bg=C["row_alt"], padx=10, pady=8)
        info_fr.pack(fill="x")
        self._calib_info[idx] = tk.Label(info_fr,
                                          text="Run calibration to start",
                                          font=self.f_small,
                                          bg=C["row_alt"], fg=C["text_dim"],
                                          wraplength=280, justify="left")
        self._calib_info[idx].pack()

        cf_row = tk.Frame(parent, bg=C["panel"])
        cf_row.pack(fill="x", pady=4)
        tk.Label(cf_row, text="Current Factor:",
                 font=self.f_label, bg=C["panel"],
                 fg=C["text_dim"]).pack(side="left")
        tk.Label(cf_row, textvariable=self._calib_factor[idx],
                 font=("Consolas",12,"bold"),
                 bg=C["panel"], fg=C["accent"]).pack(side="left", padx=6)
        tk.Label(cf_row, text="(1.0 = no correction)",
                 font=("Segoe UI",8),
                 bg=C["panel"], fg=C["text_dim"]).pack(side="left")

    def _run_calibration(self, idx):
        pump = self.pump1 if idx == 0 else self.pump2
        if not pump or not pump.is_connected():
            messagebox.showwarning("Not Connected",
                "Connect Channel " + str(idx+1) + " first.")
            return
        locked = self._motor_locked_by[idx]
        if locked and locked not in (None, "calibration"):
            messagebox.showwarning("Motor Busy",
                "Motor controlled by " + str(locked) + ". Stop it there first.")
            return
        self._motor_locked_by[idx] = "calibration"
        vol  = self._cal_vol[idx].get()
        rpm  = self._rpm_var[idx].get()
        tube = self._tube_var[idx].get()
        fwd  = (self._global_direction[idx].get() == "CW")
        t    = self._cal_time[idx].get() if self._cal_time[idx].get() > 0 else calc_run_time(tube, rpm, vol)
        flow = calc_flow_rate(tube, rpm)
        if self._cal_actual[idx]:
            self._cal_actual[idx].set(vol)
        info = "Running {:.2f} mL @ {:.1f} RPM | Time: {:.2f}s | Flow: {:.3f} mL/min | {} | {}".format(
            vol, rpm, t, flow, tube, "CW" if fwd else "CCW")
        self._calib_info[idx].config(text=info)
        def run():
            pump.set_speed(rpm)
            pump.set_direction(fwd)
            time.sleep(0.1)
            pump.start()
            time.sleep(t)
            pump.stop()
            self._motor_locked_by[idx] = None
            self.after(0, lambda: self._update_motor_ui(idx))
            self.after(0, lambda: self._calib_info[idx].config(
                text="Done! Measure actual liquid, enter below, click APPLY.",
                fg=C["green"]))
        threading.Thread(target=run, daemon=True).start()

    def _reset_calibration(self, idx):
        self._calib_factor[idx].set(1.0)
        if hasattr(self, "_cal_actual") and self._cal_actual[idx]:
            self._cal_actual[idx].set(self._cal_vol[idx].get())
        self._settings["calib" + str(idx+1)] = 1.0
        save_settings(self._settings)
        if self._calib_info[idx]:
            self._calib_info[idx].config(text="Reset. Factor = 1.0 (no correction)", fg=C["text_dim"])

    def _adj_volume(self, idx, delta):
        v = round(self._cal_adj[idx].get() + delta, 2)
        self._cal_adj[idx].set(v)

    def _adj_actual(self, idx, delta):
        v = round(self._cal_actual[idx].get() + delta, 2)
        self._cal_actual[idx].set(max(0.01, v))

    def _apply_calib_factor(self, idx):
        target = self._cal_vol[idx].get()
        actual = self._cal_actual[idx].get()
        if actual <= 0:
            messagebox.showerror("Error", "Actual volume must be greater than 0.")
            return
        factor = round(target / actual, 4)
        self._calib_factor[idx].set(factor)
        self._settings["calib" + str(idx+1)] = factor
        save_settings(self._settings)
        info = "Calibration applied! Target: {:.3f} mL, Actual: {:.3f} mL, Factor: {:.4f}".format(target, actual, factor)
        messagebox.showinfo("Calibration Applied", info)
        if self._calib_info[idx]:
            self._calib_info[idx].config(
                text="Factor: {:.4f} | Target: {:.2f} mL | Actual: {:.2f} mL".format(factor, target, actual),
                fg=C["green"])

    # ------------------------------------------------------------------
    # TAB 5 — RECIPES (Common Mode)
    # ------------------------------------------------------------------
    def _build_tab_common_mode(self):
        tab = self._tab_frame("Common Mode")
        outer, body = self._card(tab, "COMMON MODE  —  Recipe Programs")
        outer.pack(fill="both", expand=True, padx=8, pady=8)

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
        self._recipe_tree.bind("<Double-1>", self._on_recipe_row_click)
        self._recipe_tree.bind("<Return>",   self._on_recipe_row_click)

        self._recipes = self._settings.get("recipes", [])
        self._refresh_recipe_tree()

        btn_row = tk.Frame(body, bg=C["panel"])
        btn_row.pack(fill="x", pady=6)

        self._big_btn(btn_row, "  ADD",              self._add_recipe,         C["green"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  DELETE",           self._del_recipe,         C["red"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  CLEAR",            self._clear_recipes,      C["orange"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  LOAD FROM DASHBOARD", self._load_from_dashboard, C["teal"]).pack(side="left", padx=(0,4))
        self._big_btn(btn_row, "  RUN ALL",          self._run_recipes,        C["accent"]).pack(side="right")

    def _refresh_recipe_tree(self):
        for row in self._recipe_tree.get_children():
            self._recipe_tree.delete(row)
        for i, r in enumerate(self._recipes):
            tag = "evenrow" if i % 2 == 0 else ""
            # Use .get() with defaults for every key — recipes saved by
            # _add_recipe may be missing 'time', 'repeat', 'tube', 'suckback'
            tube  = r.get("tube", "2x1mm")
            vol   = r.get("vol", 0.0)
            speed = r.get("speed", 60.0)
            # 'time' was never stored by _add_recipe — calculate it on the fly
            t     = r.get("time", calc_run_time(tube, speed, vol))
            self._recipe_tree.insert("", "end", values=(
                i+1,
                f"Pump {r.get('channel', 1)}",
                tube,
                f"{vol:.2f}",
                f"{t:.2f}",
                f"{r.get('pause', 1.0):.2f}",
                r.get("repeat", 1),
                f"{speed:.1f}",
                f"{r.get('suckback', 0.0):.1f} deg",
            ), tags=(tag,))

    def _add_recipe(self):
        dlg = tk.Toplevel(self)
        dlg.title("Add Program — Common Mode")
        dlg.geometry("420x480")
        dlg.configure(bg=C["panel"])
        dlg.resizable(False, False)
        dlg.grab_set()

        hdr = tk.Frame(dlg, bg=C["accent"], pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="  ADD PROGRAM", font=self.f_bold,
                 bg=C["accent"], fg="white").pack(side="left", padx=10)

        body = tk.Frame(dlg, bg=C["panel"], padx=16, pady=10)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Select Channel:", font=self.f_bold,
                 bg=C["panel"], fg=C["text"]).grid(row=0, column=0,
                 columnspan=4, sticky="w", pady=(0,6))

        ch_var = tk.IntVar(value=1)
        def on_channel_change():
            ch = ch_var.get()
            rpm_val = int(self._rpm_var[ch-1].get()) if self._rpm_var[ch-1] else 60
            vol_val = self._dv_vol[ch-1].get() if self._dv_vol[ch-1] else 10.0
            pause_v = self._dv_pause[ch-1].get() if self._dv_pause[ch-1] else 1.0
            rep_v   = self._dv_rep[ch-1].get() if self._dv_rep[ch-1] else 1
            sb_v    = self._suckback_var[ch-1].get()
            tube_v  = self._tube_var[ch-1].get()
            try:
                fields["speed"].set(str(rpm_val))
                fields["vol"].set(str(vol_val))
                fields["pause"].set(str(pause_v))
                fields["repeat"].set(str(int(rep_v)))
                fields["suckback"].set(str(sb_v))
                fields["tube"].set(tube_v)
            except Exception:
                pass

        for ch, col in [(1, 1), (2, 3)]:
            rb = tk.Radiobutton(body, text=f"  PUMP {ch}  ",
                                variable=ch_var, value=ch,
                                font=("Segoe UI", 12, "bold"),
                                bg=C["accent"], fg="white",
                                selectcolor=C["green"],
                                activebackground=C["accent"],
                                relief="flat", padx=16, pady=8,
                                indicatoron=False,
                                command=on_channel_change)
            rb.grid(row=0, column=col, padx=6, pady=4)

        sep = tk.Frame(body, bg=C["border"], height=1)
        sep.grid(row=1, column=0, columnspan=4, sticky="ew", pady=8)

        try:
            _ch   = ch_var.get() - 1
            _rpm  = int(self._rpm_var[0].get())  if self._rpm_var[0]  else 60
            _vol  = self._dv_vol[0].get()         if self._dv_vol[0]  else 10.0
            _paus = self._dv_pause[0].get()       if self._dv_pause[0] else 1.0
            _rep  = int(self._dv_rep[0].get())    if self._dv_rep[0]  else 1
            _sb   = self._suckback_var[0].get()
            _tube = self._tube_var[0].get()
        except Exception:
            _rpm,_vol,_paus,_rep,_sb,_tube = 60,10.0,1.0,1,0.0,"2x1mm"

        fields = {}
        rows = [
            ("tube",    "Tube Size:",       _tube),
            ("vol",     "Disp. Vol. (mL):", str(_vol)),
            ("time",    "Disp. Time (s):",  "2.0"),
            ("pause",   "Pause Time (s):",  str(_paus)),
            ("repeat",  "Repeat:",          str(_rep)),
            ("speed",   "Speed (RPM):",     str(_rpm)),
            ("suckback","Suck-Back (deg):", str(_sb)),
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
                    "vol":      float(fields["vol"].get()),
                    "pause":    float(fields["pause"].get()),
                    "speed":    float(fields["speed"].get()),
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

    def _on_recipe_row_click(self, event=None):
        sel = self._recipe_tree.selection()
        if not sel:
            return
        idx_row = self._recipe_tree.index(sel[0])
        if idx_row >= len(self._recipes):
            return
        r   = self._recipes[idx_row]
        ch  = r.get("channel", 1) - 1

        try:
            if self._dv_vol[ch]:
                self._dv_vol[ch].set(r.get("vol", 10.0))
            if self._dv_pause[ch]:
                self._dv_pause[ch].set(r.get("pause", 1.0))
            if self._dv_rep[ch]:
                self._dv_rep[ch].set(r.get("repeat", 1))
            if self._rpm_var[ch]:
                self._rpm_var[ch].set(int(r.get("speed", 60)))
            if self._tube_var[ch]:
                self._tube_var[ch].set(r.get("tube", "2x1mm"))
                self._on_tube_change(ch)
            if self._suckback_var[ch]:
                self._suckback_var[ch].set(r.get("suckback", 0.0))
            self._update_calc_time(ch)

            msg = ("Program {} applied to Pump {} Dispensing!  "
                   "Vol:{:.2f}mL | Speed:{}RPM | Pause:{:.1f}s | Repeat:{}").format(
                       idx_row+1, ch+1,
                       r.get("vol",10.0), int(r.get("speed",60)),
                       r.get("pause",1.0), r.get("repeat",1))
            messagebox.showinfo("Applied", msg)
            self._nb.select(1)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _load_from_dashboard(self):
        for i in range(2):
            rpm   = self._rpm_var[i].get() if self._rpm_var[i] else 60.0
            tube  = self._tube_var[i].get()
            vol   = 10.0
            rec = {
                "channel":  i + 1,
                "tube":     tube,
                "vol":      vol,
                "time":     round(calc_run_time(tube, rpm, vol), 2),
                "pause":    1.0,
                "repeat":   1,
                "speed":    rpm,
                "suckback": self._suckback_var[i].get(),
            }
            self._recipes.append(rec)
        self._settings["recipes"] = self._recipes
        save_settings(self._settings)
        self._refresh_recipe_tree()
        messagebox.showinfo("Loaded", "Dashboard settings loaded into Common Mode!")

    def _run_recipes(self):
        if not self._recipes:
            messagebox.showwarning("Empty", "No programs to run.")
            return
        def run_all():
            for r in self._recipes:
                pump  = self.pump1 if r["channel"] == 1 else self.pump2
                idx   = r["channel"] - 1
                if not pump or not pump.is_connected():
                    continue
                tube   = r.get("tube", "2x1mm")
                speed  = r.get("speed", 60.0)
                vol    = r.get("vol", 10.0)
                pause  = r.get("pause", 1.0)
                repeat = r.get("repeat", 1)
                sb     = r.get("suckback", 0.0)
                calib  = self._calib_factor[idx].get()
                run_t  = calc_run_time(tube, speed, vol) * calib
                fwd    = (self._global_direction[idx].get() == "CW")
                for _ in range(repeat):
                    pump.set_speed(speed)
                    pump.set_direction(fwd)
                    pump.start()
                    time.sleep(run_t)
                    pump.stop()
                    if sb > 0 and speed > 0:
                        sb_time = (sb / 360.0) * (60.0 / speed)
                        pump.set_direction(not fwd)
                        pump.start()
                        time.sleep(sb_time)
                        pump.stop()
                        pump.set_direction(fwd)
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

        outer, body = self._card(fr, "COMMUNICATIONS")
        outer.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        tk.Label(body, text="COM Port:", font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")
        tk.Entry(body, textvariable=self._port_var, font=self.f_mono,
                 bg=C["input_bg"], fg="white",
                 insertbackground="white", width=14).pack(anchor="w", pady=4)

        tk.Label(body, text="Baud Rate: 9600  |  Parity: Even  |  Stop: 1",
                 font=self.f_small, bg=C["panel"], fg=C["text_dim"]).pack(anchor="w", pady=2)

        self._tube_info_lbl = [None, None]
        for i in range(2):
            sep = tk.Frame(body, bg=C["border"], height=1)
            sep.pack(fill="x", pady=6)
            tk.Label(body, text=f"Channel {i+1} Settings:",
                     font=self.f_bold, bg=C["panel"], fg=C["text"]).pack(anchor="w", pady=(0,4))

            grid = tk.Frame(body, bg=C["panel"])
            grid.pack(fill="x")

            col1 = tk.Frame(grid, bg=C["panel"], padx=(0), pady=0)
            col1.pack(side="left", padx=(0,12))
            tk.Label(col1, text="Pump ID:", font=self.f_label,
                     bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")
            id_fr = tk.Frame(col1, bg=C["border"], padx=10, pady=8)
            id_fr.pack(anchor="w")
            tk.Label(id_fr, textvariable=self._slave_var[i],
                     font=("Consolas", 18, "bold"),
                     bg=C["border"], fg=C["text"], width=2,
                     anchor="center").pack()
            tk.Label(col1, text="(fixed)", font=("Segoe UI", 8),
                     bg=C["panel"], fg=C["text_dim"]).pack()

            col2 = tk.Frame(grid, bg=C["panel"])
            col2.pack(side="left", padx=(0,12))
            tk.Label(col2, text="Tube Size:", font=self.f_label,
                     bg=C["panel"], fg=C["text_dim"]).pack(anchor="w")
            cb = ttk.Combobox(col2, textvariable=self._tube_var[i],
                              values=list(TUBE_DATA.keys()),
                              width=9, state="readonly")
            cb.pack(anchor="w", pady=2)
            cb.bind("<<ComboboxSelected>>",
                    lambda e, idx=i: self._on_tube_change(idx))
            self._tube_info_lbl[i] = tk.Label(col2, text="",
                                               font=("Segoe UI",8),
                                               bg=C["panel"], fg=C["accent"])
            self._tube_info_lbl[i].pack(anchor="w")

            col3 = tk.Frame(grid, bg=C["panel"])
            col3.pack(side="left", fill="x", expand=True)
            tk.Label(col3, text="Suck-Back Angle:",
                     font=self.f_label, bg=C["panel"],
                     fg=C["text_dim"]).pack(anchor="w")
            sb_row = tk.Frame(col3, bg=C["panel"])
            sb_row.pack(anchor="w")
            sl = tk.Scale(sb_row, from_=0, to=360, orient="horizontal",
                          variable=self._suckback_var[i], resolution=1,
                          bg=C["panel"], fg=C["text"],
                          troughcolor=C["bg"], highlightthickness=0, length=120)
            sl.pack(side="left")
            vf = tk.Frame(sb_row, bg=C["input_bg"], padx=5, pady=3)
            vf.pack(side="left", padx=4)
            tk.Label(vf, textvariable=self._suckback_var[i],
                     font=("Consolas",11,"bold"),
                     bg=C["input_bg"], fg="white", width=4).pack(side="left")
            tk.Label(vf, text="deg", font=self.f_small,
                     bg=C["input_bg"], fg="#90CAF9").pack(side="left")
            pf = tk.Frame(col3, bg=C["panel"])
            pf.pack(anchor="w", pady=2)
            for ang in [0, 45, 90, 180, 270, 360]:
                tk.Button(pf, text=str(ang),
                          command=lambda a=ang, idx=i: self._suckback_var[idx].set(a),
                          font=("Segoe UI",8), bg=C["bg"], fg=C["text"],
                          relief="flat", padx=4, pady=1,
                          cursor="hand2").pack(side="left", padx=1)
            tk.Label(col3, text="Auto runs opposite to motor direction",
                     font=("Segoe UI",8), bg=C["panel"],
                     fg=C["text_dim"]).pack(anchor="w")

        sep_dir = tk.Frame(body, bg=C["border"], height=1)
        sep_dir.pack(fill="x", pady=6)
        tk.Label(body, text="Motor Direction (applies to ALL pages):",
                 font=self.f_bold, bg=C["panel"], fg=C["text"]).pack(anchor="w", pady=(0,4))
        for i in range(2):
            dir_row = tk.Frame(body, bg=C["panel"])
            dir_row.pack(fill="x", pady=2)
            tk.Label(dir_row, text=f"Pump {i+1}:",
                     font=self.f_label, bg=C["panel"],
                     fg=C["text_dim"], width=8, anchor="w").pack(side="left")
            for val, lbl in [("CW", "CW (Reverse)"), ("CCW", "CCW (Forward)")]:
                tk.Radiobutton(dir_row, text=lbl,
                               variable=self._global_direction[i], value=val,
                               font=self.f_label, bg=C["panel"], fg=C["text"],
                               selectcolor=C["bg"],
                               activebackground=C["panel"],
                               command=lambda idx=i: self._apply_global_direction(idx)
                               ).pack(side="left", padx=8)

        sep_dir2 = tk.Frame(body, bg=C["border"], height=1)
        sep_dir2.pack(fill="x", pady=6)

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

        self._big_btn(body2, "  SAVE SETTINGS",
                      self._save_all_settings, C["accent"]).pack(fill="x", pady=4)

    # ------------------------------------------------------------------
    # Connection Logic
    # ------------------------------------------------------------------
    def _connect_all(self):
        port = self._port_var.get().strip()
        self._conn_lbl.config(text="● CONNECTING...", fg="#FFD740")

        def connect():
            SharedModbusClient.reset()
            time.sleep(0.2)

            shared = SharedModbusClient.get()
            ok = shared.connect(port=port)

            if ok:
                self.pump1 = PumpDriver(slave_id=self._slave_var[0].get())
                self.pump2 = PumpDriver(slave_id=self._slave_var[1].get())
                self.pump1.connect()
                self.pump2.connect()
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
                        "Could not open " + port + ". Check: COM port, USB adapter, driver.")

            self.after(0, update_ui)

        threading.Thread(target=connect, daemon=True).start()

    def _start_watchdog(self):
        def watch():
            while True:
                time.sleep(2.0)
                shared = SharedModbusClient.get()
                if not shared.is_connected():
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
        self._update_calc_time(idx)

    def _set_disp_speed(self, idx, rpm):
        self._dv_speed[idx].set(rpm)
        self._update_calc_time(idx)

    def _update_calc_time(self, idx):
        try:
            vol   = self._dv_vol[idx].get()
            speed = int(self._rpm_var[idx].get()) if self._rpm_var[idx] else 60
            tube  = self._tube_var[idx].get()
            calib = self._calib_factor[idx].get()
            if speed <= 0:
                return
            flow  = calc_flow_rate(tube, speed)
            run_t = calc_run_time(tube, speed, vol) * calib

            # ── FIX: Validate if volume is physically achievable ──────────
            # Check if requested volume is impossible at this tube/speed
            max_possible_flow = TUBE_DATA[tube]["max_flow"]  # at 350 RPM
            max_possible_per_min = max_possible_flow  # mL/min at max RPM
            # If user expects to dispense vol in < 1s but flow too low → warn
            # We warn when calculated run_time > 600s (10 min) — might be intentional,
            # or when volume requires more than max possible flow
            min_time_at_max_rpm = (vol / max_possible_flow) * 60.0  # seconds
            if min_time_at_max_rpm > 0:
                msg = ("Vol: {:.2f} mL  |  Speed: {} RPM  |  "
                       "Flow: {:.3f} mL/min  |  Run time: {:.2f} s").format(
                           vol, speed, flow, run_t)
                # Append warning if requested run is under minimum possible time
                dv_time_val = self._dv_time[idx].get() if self._dv_time[idx] else 0
                if dv_time_val > 0 and dv_time_val < min_time_at_max_rpm:
                    msg += "  ⚠ NOT POSSIBLE at any speed — increase time or reduce volume"
                    if hasattr(self, "_calc_time_lbl") and self._calc_time_lbl[idx]:
                        self._calc_time_lbl[idx].config(text=msg, fg=C["red"])
                        return
            if hasattr(self, "_calc_time_lbl") and self._calc_time_lbl[idx]:
                self._calc_time_lbl[idx].config(text=msg, fg=C["accent"])
            if hasattr(self, "_disp_rpm_display") and self._disp_rpm_display[idx]:
                self._disp_rpm_display[idx].config(text=str(speed))
        except Exception:
            pass

    def _update_tube_labels(self):
        for i in range(2):
            tube = self._tube_var[i].get()
            data = TUBE_DATA.get(tube, {})
            rpm  = self._rpm_var[i].get() if hasattr(self, "_rpm_var") and self._rpm_var[i] else 0
            flow = calc_flow_rate(tube, rpm)
            if hasattr(self, "_tube_lbl") and self._tube_lbl[i]:
                self._tube_lbl[i].config(text=data.get("label", tube))
            if hasattr(self, "_flow_lbl") and self._flow_lbl[i]:
                self._flow_lbl[i].config(
                    text=f"{flow:.3f} mL/min" if rpm > 0 else f"Max: {data.get('max_flow',0):.2f} mL/min")
            if hasattr(self, "_rpm_lbl") and self._rpm_lbl[i]:
                self._rpm_lbl[i].config(text=f"{rpm:.1f} RPM")

    def _on_tube_change(self, idx):
        tube = self._tube_var[idx].get()
        data = TUBE_DATA.get(tube, {})
        self._update_tube_labels()
        if hasattr(self, "_tube_info_lbl") and self._tube_info_lbl[idx]:
            self._tube_info_lbl[idx].config(
                text=f"Max: {data.get('max_flow',0):.2f} mL/min @ 350 RPM")
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
        if pump.is_running and self._motor_locked_by[idx] == "dashboard":
            return
        if not self._check_lock(idx, "dashboard"):
            return
        self._motor_locked_by[idx] = "dashboard"
        def run():
            rpm = int(self._rpm_var[idx].get())
            fwd = (self._global_direction[idx].get() == "CW")
            pump.set_speed(rpm)
            time.sleep(0.15)
            pump.set_direction(fwd)
            time.sleep(0.15)
            pump.start()
            self.after(0, lambda: self._update_motor_ui(idx))
            self.after(100, lambda i=idx, r=rpm: self._start_dashboard_volume_tracking(i, r))
        threading.Thread(target=run, daemon=True).start()

    def _cmd_stop(self, idx):
        pump = self._get_pump(idx)
        if self._motor_locked_by[idx] == "dashboard":
            self._motor_locked_by[idx] = None
        self._stop_dashboard_volume_tracking(idx)
        def force_stop():
            if pump:
                for _ in range(3):
                    try:
                        SharedModbusClient.get().write_register(1000, 0, pump.slave_id)
                    except: pass
                    time.sleep(0.05)
                pump._running = False
            self.after(0, lambda: self._update_motor_ui(idx))
        threading.Thread(target=force_stop, daemon=True).start()

    def _set_rpm(self, idx, rpm):
        rpm = int(rpm)
        self._rpm_var[idx].set(rpm)
        pump = self._get_pump(idx)
        if pump and pump.is_connected():
            def send(r=rpm, p=pump):
                try: p.set_speed(r)
                except: pass
            threading.Thread(target=send, daemon=True).start()
        tube = self._tube_var[idx].get()
        flow = calc_flow_rate(tube, rpm)
        if hasattr(self, "_flow_lbl") and self._flow_lbl[idx]:
            self._flow_lbl[idx].config(text=f"{flow:.3f} mL/min")
        if hasattr(self, "_rpm_lbl") and self._rpm_lbl[idx]:
            self._rpm_lbl[idx].config(text=f"{rpm} RPM")

    def _rpm_entry_changed(self, idx):
        try:
            rpm = int(self._rpm_var[idx].get())
            rpm = max(1, min(350, rpm))
            self._rpm_var[idx].set(rpm)
            self._on_rpm_change(idx)
        except (ValueError, tk.TclError):
            self._rpm_var[idx].set(60)

    def _on_rpm_change(self, idx):
        pump = self._get_pump(idx)
        try:
            rpm = int(self._rpm_var[idx].get())
            rpm = max(1, min(350, rpm))
        except (ValueError, tk.TclError):
            rpm = 60
        if pump and pump.is_connected():
            def send_rpm(r=rpm, p=pump):
                try: p.set_speed(r)
                except: pass
            threading.Thread(target=send_rpm, daemon=True).start()
        tube = self._tube_var[idx].get()
        flow = calc_flow_rate(tube, rpm)
        if hasattr(self, "_flow_lbl") and self._flow_lbl[idx]:
            self._flow_lbl[idx].config(text=f"{flow:.3f} mL/min")
        if hasattr(self, "_rpm_lbl") and self._rpm_lbl[idx]:
            self._rpm_lbl[idx].config(text=f"{rpm} RPM")

    def _set_direction(self, idx):
        d = self._dir_var[idx].get()
        self._global_direction[idx].set(d)
        pump = self._get_pump(idx)
        if pump and pump.is_connected():
            fwd = (d == "CW")
            threading.Thread(target=lambda: pump.set_direction(fwd),
                             daemon=True).start()
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
        running  = pump.is_running
        locked   = self._motor_locked_by[idx]
        color    = C["green"] if running else C["red"]

        if hasattr(self, "_status_dot") and self._status_dot[idx]:
            self._status_dot[idx].config(fg=color)

        if hasattr(self, "_status_txt") and self._status_txt[idx]:
            if running and locked:
                txt = f"RUNNING ({locked.upper()})"
            elif running:
                txt = "RUNNING"
            else:
                txt = "STOPPED"
            self._status_txt[idx].config(text=txt, fg=color)

    # ------------------------------------------------------------------
    # ═══════════════════════════════════════════════════════════════════
    #  DISPENSING LOGIC — ALL 4 BUGS FIXED HERE
    # ═══════════════════════════════════════════════════════════════════
    # ------------------------------------------------------------------
    def _run_dispense(self, idx):
        pump = self._get_pump(idx)
        if not pump or not pump.is_connected():
            messagebox.showwarning("Not Connected",
                                   f"Connect Channel {idx+1} in Settings first.")
            return

        if not self._dispensing_active[idx].get():
            messagebox.showwarning("Dispensing OFF",
                "Turn ON the Dispensing toggle first (top right of panel).")
            return

        # ── FIX 3: Allow re-run after task completes — clear finished event ──
        # If the previous stop_event is already set (task completed or stopped),
        # just clear it so we can start fresh. Only block if a thread is STILL
        # actively running (event not yet set means thread is mid-run).
        existing_ev = self._stop_events.get(idx)
        if existing_ev is not None and not existing_ev.is_set():
            # Thread is still running — do not start a new one
            return

        if not self._check_lock(idx, "dispensing"):
            return

        self._motor_locked_by[idx] = "dispensing"

        # ── FIX 1: No blocking stop+sleep in UI thread.
        # We only signal stop to any leftover thread; actual pump stop
        # happens inside the new thread before starting the motor.
        if existing_ev is not None:
            existing_ev.set()  # signal old thread to quit (already done, but safe)

        vol    = self._dv_vol[idx].get()
        pause  = self._dv_pause[idx].get()
        repeat = self._dv_rep[idx].get()
        speed  = int(self._rpm_var[idx].get())
        tube   = self._tube_var[idx].get()
        calib  = self._calib_factor[idx].get()

        # ── FIX 2: Validate impossible volume/time BEFORE starting ──────────
        if speed <= 0:
            messagebox.showerror("Invalid Speed", "Speed must be > 0 RPM.")
            self._motor_locked_by[idx] = None
            return
        flow = calc_flow_rate(tube, speed)
        if flow <= 0:
            messagebox.showerror("Invalid", "Flow rate is 0. Check tube and speed settings.")
            self._motor_locked_by[idx] = None
            return

        # Check requested time vs physically possible minimum time
        requested_time = self._dv_time[idx].get()  # user's "Disp. Time" field (informational)
        calc_time = calc_run_time(tube, speed, vol) * calib  # actual time needed

        # Max possible flow at 350 RPM for this tube
        max_flow_at_max_rpm = TUBE_DATA[tube]["max_flow"]  # mL/min at 350 RPM
        min_possible_time   = (vol / max_flow_at_max_rpm) * 60.0  # seconds

        if requested_time > 0 and requested_time < min_possible_time:
            messagebox.showerror(
                "Not Possible",
                f"Cannot dispense {vol:.2f} mL in {requested_time:.1f} s with tube {tube}.\n\n"
                f"Minimum possible time at 350 RPM (max speed) = {min_possible_time:.1f} s\n"
                f"At your current {speed} RPM, it needs {calc_time:.1f} s.\n\n"
                f"Options:\n"
                f"  • Reduce volume below {max_flow_at_max_rpm * (requested_time/60):.2f} mL\n"
                f"  • Increase time to at least {min_possible_time:.1f} s\n"
                f"  • Use a larger tube (e.g. 4x1mm max {TUBE_DATA['4x1mm']['max_flow']:.1f} mL/min)"
            )
            self._motor_locked_by[idx] = None
            return

        # Create new stop event
        stop_ev = threading.Event()
        self._stop_events[idx] = stop_ev

        def run():
            # ── FIX 1 cont.: Stop motor at start of thread (not blocking UI) ─
            try:
                pump.stop()
            except: pass
            time.sleep(0.05)  # tiny settle, non-blocking to UI

            for cycle in range(repeat):
                if stop_ev.is_set():
                    break

                # Always read LATEST settings at start of each cycle
                current_tube  = self._tube_var[idx].get()
                current_speed = int(self._rpm_var[idx].get())
                current_calib = self._calib_factor[idx].get()
                # ── FIX 2: run_t is computed HERE inside the thread, from fresh values ──
                current_run_t = calc_run_time(current_tube, current_speed, vol) * current_calib

                self.after(0, lambda c=cycle, t=current_tube: (
                    self._disp_counter[idx].config(text=f"{c+1} / {repeat}"),
                    self._disp_status[idx].config(
                        text=f"Dispensing {vol:.2f} mL  [{t}]...")
                ))

                actual_fwd = (self._global_direction[idx].get() == "CW")
                pump.set_speed(current_speed)
                pump.set_direction(actual_fwd)
                time.sleep(0.05)  # reduced settle: 50ms not 100ms
                pump.start()
                self.after(0, lambda: self._update_motor_ui(idx))

                start = time.time()
                while True:
                    if stop_ev.is_set():
                        break
                    elapsed = time.time() - start
                    if elapsed >= current_run_t:
                        break
                    pct       = min((elapsed / current_run_t) * 100, 100) if current_run_t > 0 else 100
                    dispensed = round(calc_volume(current_tube, current_speed, elapsed) * current_calib, 3)
                    total_now = round(self._total_vol[idx] + dispensed, 3)
                    def _upd(p=pct, d=total_now, i=idx):
                        try:
                            self._disp_prog[i].configure(value=p)
                            self._disp_volume[i].set(d)
                            self._disp_status[i].config(
                                text=f"Dispensing... {d:.3f} mL")
                        except: pass
                    self.after(0, _upd)
                    time.sleep(0.05)  # reduced poll: 50ms for tighter timing

                pump.stop()

                # Suck-back
                sb_angle = float(self._suckback_var[idx].get())
                if sb_angle > 0 and current_speed > 0:
                    sb_time = (sb_angle / 360.0) * (60.0 / current_speed)
                    time.sleep(0.05)
                    pump.set_direction(not actual_fwd)
                    time.sleep(0.05)
                    pump.start()
                    time.sleep(sb_time)
                    pump.stop()
                    time.sleep(0.05)
                    pump.set_direction(actual_fwd)

                self.after(0, lambda: self._update_motor_ui(idx))

                self._total_vol[idx] += vol
                self.after(0, lambda v=self._total_vol[idx]: (
                    self._disp_volume[idx].set(round(v, 3))
                ))

                if cycle < repeat - 1 and not stop_ev.is_set():
                    self.after(0, lambda: self._disp_status[idx].config(
                        text=f"Pausing {pause:.1f} s..."))
                    time.sleep(pause)

            # ── Task complete: mark stop_ev as set so re-run is allowed ──────
            stop_ev.set()  # FIX 3: signal done so next START click is not blocked
            self._motor_locked_by[idx] = None

            self.after(0, lambda: (
                self._disp_prog[idx].configure(value=0),
                self._disp_status[idx].config(
                    text="Complete! Ready for next run." if not stop_ev.is_set() else "Complete! Ready for next run."),
                self._disp_counter[idx].config(
                    text=f"{repeat} / {repeat}")
            ))
            self.after(0, lambda: self._update_motor_ui(idx))

        t = threading.Thread(target=run, daemon=True)
        self._threads[idx] = t
        t.start()

    def _stop_dispense(self, idx):
        ev = self._stop_events.get(idx)
        if ev:
            ev.set()
        self._motor_locked_by[idx] = None
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
        port = self._port_var.get().strip()
        self._test_result.config(text=f"Testing {port}...", fg=C["text_dim"])

        def test():
            import io, sys
            result = "FAIL: unknown error"
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

            # ── FIX: was missing else — always showed fail message regardless ──
            def show():
                if result == "OK":
                    msg = "Port " + port + " opened OK! Now click CONNECT BOTH"
                    self._test_result.config(text=msg, fg=C["green"])
                else:
                    msg = "Port test failed: " + result + ". Try: Unplug/replug USB, check Device Manager."
                    self._test_result.config(text=msg, fg=C["red"])
            self.after(0, show)

        threading.Thread(target=test, daemon=True).start()

    def _start_dashboard_volume_tracking(self, idx, rpm=None):
        pump = self._get_pump(idx)

        self._total_vol[idx] = 0.0
        self._disp_volume[idx].set(0.0)

        stop_key = "_dash_vol_stop_" + str(idx)
        ev = getattr(self, stop_key, None)
        if ev: ev.set()
        stop_ev = threading.Event()
        setattr(self, stop_key, stop_ev)

        def track():
            total_vol = 0.0
            last_t    = time.time()
            while not stop_ev.is_set():
                if not pump or not pump.is_running:
                    break
                now   = time.time()
                dt    = now - last_t
                last_t = now

                try:
                    cur_rpm = int(self._rpm_var[idx].get())
                except Exception:
                    cur_rpm = 60
                tube  = self._tube_var[idx].get()
                calib = self._calib_factor[idx].get()

                flow       = calc_flow_rate(tube, cur_rpm) * calib
                total_vol += flow * (dt / 60.0)
                total_vol  = round(total_vol, 3)

                self.after(0, lambda v=total_vol: self._disp_volume[idx].set(v))
                time.sleep(0.2)

        threading.Thread(target=track, daemon=True).start()

    def _stop_dashboard_volume_tracking(self, idx):
        stop_key = "_dash_vol_stop_" + str(idx)
        ev = getattr(self, stop_key, None)
        if ev: ev.set()

    def _check_lock(self, idx, caller):
        locked = self._motor_locked_by[idx]

        disp_on = (hasattr(self, "_dispensing_active") and
                   self._dispensing_active[idx].get())
        if disp_on and caller != "dispensing":
            messagebox.showwarning("Dispensing Active",
                "Pump " + str(idx+1) + " is in Dispensing mode! "
                "Turn OFF the Dispensing toggle first.")
            return False

        timer_key = "_timer_stop_" + str(idx)
        timer_ev = getattr(self, timer_key, None)
        timer_on = timer_ev is not None and not timer_ev.is_set()
        if timer_on and caller != "timing":
            messagebox.showwarning("Timer Active",
                "Pump " + str(idx+1) + " has an active timer! "
                "Cancel the timer in Timing tab first.")
            return False

        if locked and locked != caller:
            messagebox.showwarning("Motor Busy",
                "Pump " + str(idx+1) + " is running from " + str(locked) + ". Stop it there first.")
            return False

        return True

    def _apply_global_direction(self, idx):
        d = self._global_direction[idx].get()
        if hasattr(self, "_dir_var") and self._dir_var[idx]:
            self._dir_var[idx].set(d)
        pump = self._get_pump(idx)
        if pump and pump.is_connected():
            fwd = (d == "CW")
            threading.Thread(target=lambda: pump.set_direction(fwd),
                             daemon=True).start()
        if hasattr(self, "_dir_lbl") and self._dir_lbl[idx]:
            self._dir_lbl[idx].config(text=d,
                bg=C["green"] if d == "CW" else C["orange"])

    def _save_all_settings(self):
        self._settings.update({
            "port":      self._port_var.get(),
            "slave2":    self._slave_var[1].get(),
            "tube2":     self._tube_var[1].get(),
            "calib2":    self._calib_factor[1].get(),
            "suckback2": self._suckback_var[1].get(),
            "dir2":      self._global_direction[1].get(),
        })
        save_settings(self._settings)
        messagebox.showinfo("Saved", "Settings saved successfully!")

    def _on_close(self):
        for idx in range(2):
            self._stop_dispense(idx)
        self._disconnect_all()
        self._save_all_settings()
        self.destroy()


# ============================================================================
if __name__ == "__main__":
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = PumpHMI()
    app.mainloop()