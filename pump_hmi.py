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
import time
import threading
import struct
import json
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

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
        # Send stop once; retry once only on failure.
        # 3 retries could take up to 4.5s on timeout — too slow for multi-cycle dispensing.
        ok = self._write_reg(1000, 0)
        if not ok:
            try: self._write_reg(1000, 0)
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
    data = TUBE_DATA.get(tube_key) or TUBE_DATA["2x1mm"]   # safe fallback
    return (data["max_flow"] / MAX_RPM) * rpm

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
            data = json.loads(SETTINGS_FILE.read_text())
            # Sanitise numeric fields — corrupted values default to safe values
            for k, default in [("calib1",1.0),("calib2",1.0),
                                ("suckback1",0.0),("suckback2",0.0)]:
                try: data[k] = float(data[k])
                except: data[k] = default
            for k, default in [("slave1",1),("slave2",2)]:
                try: data[k] = int(data[k])
                except: data[k] = default
            for k in ["tube1","tube2"]:
                if data.get(k) not in TUBE_DATA:
                    data[k] = "2x1mm"
            return data
    except Exception:
        pass
    return {}

def save_settings(data):
    try:
        text = json.dumps(data, indent=2)
        # Write to temp file first, then rename — prevents corruption on power loss
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(SETTINGS_FILE)
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

        # ── Global input validators — registered ONCE, reused everywhere ──────
        def _vf(s):   # positive float: digits + at most one decimal, no minus
            if s == "" or s == ".":
                return True
            if s.count(".") > 1 or s.startswith("-"):
                return False
            return all(p == "" or p.isdigit() for p in s.split("."))
        def _vi(s):   # positive integer: digits only
            return s == "" or (s.isdigit() and len(s) <= 6)

        self._vcmd_float = (self.register(_vf), "%P")
        self._vcmd_int   = (self.register(_vi), "%P")

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

    def _input_box(self, parent, label, var, unit="", width=10, row=0, col=0, vcmd=None):
        tk.Label(parent, text=label, font=self.f_label,
                 bg=C["panel"], fg=C["text_dim"]).grid(
                     row=row, column=col, sticky="w", pady=3, padx=(0, 4))
        fr = tk.Frame(parent, bg=C["input_bg"], padx=4, pady=3)
        fr.grid(row=row, column=col+1, sticky="w", pady=3, padx=4)
        kw = {"validate": "key", "validatecommand": vcmd} if vcmd else {}
        e = tk.Entry(fr, textvariable=var, font=("Consolas", 13, "bold"),
                     bg=C["input_bg"], fg=C["input_fg"],
                     insertbackground="white", bd=0, width=width, **kw)
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

        self._status_dot = getattr(self, "_status_dot", [None, None])
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
        # Reuse existing DoubleVar — do NOT recreate it or dispensing tracking breaks
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
                             width=7, bd=0,
                             validate="key", validatecommand=self._vcmd_int)
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
        # Use class-level validators registered once in _build_fonts
        vcmd_float = self._vcmd_float
        vcmd_int   = self._vcmd_int

        # ── Header: title + ON/OFF toggle ────────────────────────────────────
        top_fr = tk.Frame(parent, bg=C["panel"])
        top_fr.pack(fill="x", pady=(0, 6))

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
                 fg=C["text_dim"]).pack(side="left", padx=(0, 6))

        self._disp_toggle_btn = getattr(self, "_disp_toggle_btn", [None, None])
        toggle_btn = tk.Button(on_off_fr, text="OFF",
                               font=("Segoe UI", 11, "bold"),
                               bg=C["red"], fg="white",
                               relief="flat", padx=16, pady=6,
                               cursor="hand2")
        toggle_btn.pack(side="left")
        self._disp_toggle_btn[idx] = toggle_btn

        def _make_toggle(btn, var, i):
            def toggle():
                new_val = not var.get()
                var.set(new_val)
                btn.config(text="ON" if new_val else "OFF",
                           bg=C["green"] if new_val else C["red"])
                if not new_val:
                    self._stop_dispense(i)
            return toggle
        toggle_btn.config(command=_make_toggle(toggle_btn, self._dispensing_active[idx], idx))

        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", pady=6)

        # ── Validated input fields ────────────────────────────────────────────
        # Default time: calculated so that 10 mL at 60 RPM with 2x1mm tube is achievable
        default_time = round(calc_run_time("2x1mm", 60, 10.0))  # ≈ 70 s

        self._dv_vol   = getattr(self, "_dv_vol",   [None, None])
        self._dv_time  = getattr(self, "_dv_time",  [None, None])
        self._dv_pause = getattr(self, "_dv_pause", [None, None])
        self._dv_rep   = getattr(self, "_dv_rep",   [None, None])
        self._dv_speed = getattr(self, "_dv_speed", [None, None])  # kept for compat

        self._dv_vol[idx]   = tk.DoubleVar(value=10.0)
        self._dv_time[idx]  = tk.DoubleVar(value=float(default_time))
        self._dv_pause[idx] = tk.DoubleVar(value=1.0)
        self._dv_rep[idx]   = tk.IntVar(value=1)
        self._dv_speed[idx] = tk.IntVar(value=60)   # kept for compat, not shown

        grid = tk.Frame(parent, bg=C["panel"])
        grid.pack(fill="x", pady=4)

        field_defs = [
            ("Disp. Vol.:", self._dv_vol[idx],   "mL", vcmd_float, 10),
            ("Disp. Time:", self._dv_time[idx],  "s",  vcmd_float, 10),
            ("Pause Time:", self._dv_pause[idx], "s",  vcmd_float, 10),
            ("Repeat:",     self._dv_rep[idx],   "",   vcmd_int,   10),
        ]
        for row, (lbl, var, unit, vcmd, w) in enumerate(field_defs):
            tk.Label(grid, text=lbl, font=self.f_label,
                     bg=C["panel"], fg=C["text_dim"]).grid(
                         row=row, column=0, sticky="w", pady=3, padx=(0, 4))
            fr = tk.Frame(grid, bg=C["input_bg"], padx=4, pady=3)
            fr.grid(row=row, column=1, sticky="w", pady=3, padx=4)
            tk.Entry(fr, textvariable=var,
                     font=("Consolas", 13, "bold"),
                     bg=C["input_bg"], fg=C["input_fg"],
                     insertbackground="white", bd=0, width=w,
                     validate="key", validatecommand=vcmd).pack(side="left")
            if unit:
                tk.Label(fr, text=f" {unit}", font=self.f_small,
                         bg=C["input_bg"], fg="#90CAF9").pack(side="left")

        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", pady=8)

        # ── Calculated result: Vol + Time → Required RPM ─────────────────────
        result_fr = tk.Frame(parent, bg=C["input_bg"], padx=12, pady=10)
        result_fr.pack(fill="x", pady=2)

        tk.Label(result_fr, text="REQUIRED RPM  —  calculated from Vol ÷ Time",
                 font=self.f_small, bg=C["input_bg"], fg="#90CAF9").pack(anchor="w")

        rpm_row = tk.Frame(result_fr, bg=C["input_bg"])
        rpm_row.pack(fill="x", pady=(4, 0))

        self._disp_rpm_display = getattr(self, "_disp_rpm_display", [None, None])
        self._disp_rpm_display[idx] = tk.Label(rpm_row, text="---",
                                                font=("Consolas", 32, "bold"),
                                                bg=C["input_bg"], fg="#64FFDA")
        self._disp_rpm_display[idx].pack(side="left")
        tk.Label(rpm_row, text=" RPM", font=("Segoe UI", 13),
                 bg=C["input_bg"], fg="#90CAF9").pack(side="left", pady=4)

        self._calc_time_lbl = getattr(self, "_calc_time_lbl", [None, None])
        self._calc_time_lbl[idx] = tk.Label(result_fr,
                                             text="Enter volume and time above",
                                             font=self.f_small,
                                             bg=C["input_bg"], fg="#90CAF9",
                                             wraplength=300, justify="left")
        self._calc_time_lbl[idx].pack(anchor="w", pady=(4, 0))

        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", pady=8)

        # ── Progress & status ─────────────────────────────────────────────────
        self._disp_prog = getattr(self, "_disp_prog", [None, None])
        self._disp_prog[idx] = ttk.Progressbar(parent, mode="determinate")
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

        # ── Live calculation traces ───────────────────────────────────────────
        self._dv_vol[idx].trace_add("write",  lambda *a, i=idx: self._update_calc_time(i))
        self._dv_time[idx].trace_add("write", lambda *a, i=idx: self._update_calc_time(i))
        # Direct call for initial display — bypasses debounce so it shows immediately
        parent.after(150, lambda i=idx: self._do_update_calc_time(i))

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

            _spinboxes = []
            for var, tip, maxval, sep in [
                (h_var, "HH", 23, ":"),
                (m_var, "MM", 59, ":"),
                (s_var, "SS", 59, ""),
            ]:
                sp = ttk.Spinbox(row, from_=0, to=maxval,
                                 textvariable=var,
                                 font=("Consolas", 14, "bold"),
                                 width=3, justify="center",
                                 wrap=True,
                                 validate="key",
                                 validatecommand=self._vcmd_int)
                sp.pack(side="left", padx=1)
                _spinboxes.append(sp)
                if sep:
                    tk.Label(row, text=sep, font=("Consolas", 14),
                             bg=C["panel"], fg=C["text"]).pack(side="left", padx=1)
            # Store spinbox list for locking
            setattr(self, key + "_spinboxes", _spinboxes)

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
            # Store button reference so we can lock/unlock when timer activates
            setattr(self, key + "_btn", en_btn)

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
            try:
                now_lbl.config(text=datetime.now().strftime("%H:%M:%S"))
                parent.after(1000, tick)
            except Exception:
                pass   # window destroyed — stop silently
        tick()

        btn_row = tk.Frame(parent, bg=C["panel"])
        btn_row.pack(fill="x", pady=8)
        self._big_btn(btn_row, "  ACTIVATE TIMER",
                      lambda i=idx: self._apply_timing(i),
                      C["accent"]).pack(side="left", padx=(0,6), fill="x", expand=True)

        cancel_btn = tk.Button(btn_row,
                               text="No active timer",
                               command=lambda i=idx: self._cancel_timing(i, stop_motor=True),
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
        # Unlock controls when timer completes
        self._set_timing_controls_locked(idx, False)

    def _set_timing_controls_locked(self, idx, locked):
        """Lock all timing input controls while a timer is active.
        Prevents the user from changing ON/OFF or time values mid-run."""
        state = "disabled" if locked else "normal"
        for section in ["start", "stop"]:
            key = f"_timing_{section}_{idx}"
            # Lock/unlock ON/OFF button
            btn = getattr(self, key + "_btn", None)
            if btn:
                btn.config(state=state,
                           cursor="arrow" if locked else "hand2")
            # Lock/unlock spinboxes (HH MM SS)
            for sp in getattr(self, key + "_spinboxes", []):
                try: sp.config(state=state)
                except: pass

    def _cancel_timing(self, idx, stop_motor=False):
        """Cancel the timer thread.
        stop_motor=True only when user explicitly clicks CANCEL TIMER button.
        Internal cleanup calls (from _apply_timing) must NOT stop the motor.
        """
        key = "_timer_stop_" + str(idx)
        ev = getattr(self, key, None)
        if ev:
            ev.set()
        setattr(self, key, None)
        if self._motor_locked_by[idx] == "timing":
            self._motor_locked_by[idx] = None
        # Only stop the motor when user explicitly cancels (not on internal reset)
        if stop_motor:
            self._stop_dispense(idx)
        sl = getattr(self, "_timing_status_" + str(idx), None)
        if sl: sl.config(text="Timer cancelled.", fg=C["text_dim"])
        btn = getattr(self, "_timing_cancel_btn_" + str(idx), None)
        if btn: btn.config(state="disabled", bg=C["border"], text="No active timer")
        # Unlock all timing controls
        self._set_timing_controls_locked(idx, False)

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
            proceed = messagebox.askyesno(
                "Timer Setup",
                "Stop is ON but Start is OFF.\n\n"
                "The pump will only STOP at the set time — "
                "it will NOT start automatically.\n\n"
                "Do you want to activate this stop-only timer?")
            if not proceed:
                return
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
        # No sleep here — sleeping in the UI thread freezes the window.

        stop_ev = threading.Event()
        setattr(self, "_timer_stop_" + str(idx), stop_ev)

        # Snapshot Tkinter vars in the UI thread — never read StringVar/IntVar
        # from a background thread (Tkinter is not thread-safe).
        snap_rpm = int(self._rpm_var[idx].get())
        snap_fwd = (self._global_direction[idx].get() == "CW")

        # Track the (hour, minute) the action last fired so "daily" repeats
        # correctly and "once" doesn't re-fire within the same minute.
        start_fired_min = [None]
        stop_fired_min  = [None]

        def time_in_window(h, m, s):
            """True if current wall-clock is within ±1 s of target.
            A ±1 s window prevents missing the exact second due to OS
            scheduling jitter when the loop sleeps 0.25 s per tick."""
            now_s = (datetime.now().hour * 3600
                     + datetime.now().minute * 60
                     + datetime.now().second)
            tgt_s = h * 3600 + m * 60 + s
            return abs(now_s - tgt_s) <= 1

        def watch():
            while not stop_ev.is_set():
                now     = datetime.now()
                now_min = (now.hour, now.minute)

                # ── START action ─────────────────────────────────────────
                if start_en and time_in_window(sh, sm, ss):
                    if start_fired_min[0] != now_min:
                        start_fired_min[0] = now_min
                        pump = self._get_pump(idx)
                        if pump and pump.is_connected():
                            self._motor_locked_by[idx] = "timing"
                            pump.set_speed(snap_rpm)
                            pump.set_direction(snap_fwd)
                            pump.start()
                            self.after(0, lambda i=idx: self._update_motor_ui(i))
                        if start_freq == "once" and not stop_en:
                            stop_ev.set()
                            self.after(200, lambda i=idx: self._reset_timer_ui(i))
                            return

                # ── STOP action ──────────────────────────────────────────
                if stop_en and time_in_window(eh, em, es):
                    if stop_fired_min[0] != now_min:
                        stop_fired_min[0] = now_min
                        # _stop_dispense kills the dispense thread (sets stop_ev)
                        # AND stops the hardware. Without this, the dispense thread
                        # survives the hardware stop and auto-starts the next cycle.
                        self.after(0, lambda i=idx: self._stop_dispense(i))
                        self._motor_locked_by[idx] = None
                        self.after(0, lambda i=idx: self._update_motor_ui(i))
                        if stop_freq == "once":
                            stop_ev.set()
                            self.after(200, lambda i=idx: self._reset_timer_ui(i))
                            return

                time.sleep(0.25)   # tight poll — won't miss a second

        threading.Thread(target=watch, daemon=True).start()

        parts = []
        if start_en:
            parts.append("START {:02d}:{:02d}:{:02d} ({})".format(sh,sm,ss,start_freq))
        if stop_en:
            parts.append("STOP  {:02d}:{:02d}:{:02d} ({})".format(eh,em,es,stop_freq))

        btn = getattr(self, "_timing_cancel_btn_" + str(idx), None)
        if btn: btn.config(state="normal", bg=C["red"], text="CANCEL TIMER")

        sl = getattr(self, "_timing_status_" + str(idx), None)
        if sl:
            sl.config(
                text="✔ ACTIVE: " + " | ".join(parts) + "  |  Click CANCEL TIMER to stop.",
                fg=C["green"])
        # Lock ON/OFF toggles and spinboxes — can't change while timer is running
        self._set_timing_controls_locked(idx, True)

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
        self._input_box(grid, "Target Vol.:", self._cal_vol[idx], "mL", row=0, col=0, vcmd=self._vcmd_float)
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
                 insertbackground="white", bd=0, width=8,
                 validate="key", validatecommand=self._vcmd_float).pack(side="left")
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
        # Snapshot all Tkinter vars in UI thread before spawning thread
        vol  = self._cal_vol[idx].get()
        rpm  = int(self._rpm_var[idx].get())
        tube = self._tube_var[idx].get()
        fwd  = (self._global_direction[idx].get() == "CW")
        t    = calc_run_time(tube, rpm, vol)   # always derive from vol/speed/tube
        if t <= 0:
            messagebox.showerror("Error", "Cannot calculate run time. Check RPM and tube settings.")
            self._motor_locked_by[idx] = None
            return
        flow = calc_flow_rate(tube, rpm)
        if self._cal_actual[idx]:
            self._cal_actual[idx].set(vol)
        info = "Running {:.2f} mL @ {:.1f} RPM | Time: {:.2f}s | Flow: {:.3f} mL/min | {} | {}".format(
            vol, rpm, t, flow, tube, "CW" if fwd else "CCW")
        self._calib_info[idx].config(text=info)
        def run():
            try:
                pump.set_speed(rpm)   # rpm already snapshotted in UI thread
                pump.set_direction(fwd)
                pump.start()
                time.sleep(t)
                pump.stop()
                self.after(0, lambda: self._calib_info[idx].config(
                    text="Done! Measure actual liquid, enter below, click APPLY.",
                    fg=C["green"]))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._calib_info[idx].config(
                    text=f"Error during calibration: {e}", fg=C["red"]))
            finally:
                self._motor_locked_by[idx] = None
                self.after(0, lambda i=idx: self._update_motor_ui(i))
        threading.Thread(target=run, daemon=True).start()

    def _reset_calibration(self, idx):
        self._calib_factor[idx].set(1.0)
        if hasattr(self, "_cal_actual") and self._cal_actual[idx]:
            self._cal_actual[idx].set(self._cal_vol[idx].get())
        self._settings["calib" + str(idx+1)] = 1.0
        save_settings(self._settings)
        if self._calib_info[idx]:
            self._calib_info[idx].config(text="Reset. Factor = 1.0 (no correction)", fg=C["text_dim"])
        # Refresh dispensing tab
        self._update_calc_time(idx)

    def _adj_actual(self, idx, delta):
        # Don't adjust while calibration motor is running
        if self._motor_locked_by[idx] == "calibration":
            return
        v = round(self._cal_actual[idx].get() + delta, 2)
        self._cal_actual[idx].set(max(0.01, v))

    def _apply_calib_factor(self, idx):
        try:
            target = float(self._cal_vol[idx].get())
            actual = float(self._cal_actual[idx].get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Error", "Enter valid numbers for target and actual volume.")
            return
        if actual <= 0:
            messagebox.showerror("Error", "Actual volume must be greater than 0.")
            return
        if target <= 0:
            messagebox.showerror("Error", "Target volume must be greater than 0.")
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
        # Refresh dispensing tab — run time changes with new calibration factor
        self._update_calc_time(idx)

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

        # Hint label
        hint = tk.Frame(body, bg=C["row_alt"], padx=10, pady=6)
        hint.pack(fill="x", pady=(0, 4))
        tk.Label(hint,
                 text="Select a row and click  APPLY TO DISPENSING  (or double-click) "
                      "to load settings into the pump's Dispensing page, then run from there.",
                 font=("Segoe UI", 9), bg=C["row_alt"], fg=C["text_dim"],
                 wraplength=800, justify="left").pack(anchor="w")

        btn_row = tk.Frame(body, bg=C["panel"])
        btn_row.pack(fill="x", pady=6)

        self._big_btn(btn_row, "  ADD",    self._add_recipe,    C["green"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  DELETE", self._del_recipe,    C["red"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  CLEAR",  self._clear_recipes, C["orange"]).pack(side="left", padx=(0, 4))
        self._big_btn(btn_row, "  APPLY TO DISPENSING",
                      self._on_recipe_row_click, C["accent"]).pack(side="right")

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
                # repeat = integer; all others = positive float
                _vcmd = self._vcmd_int if k == "repeat" else self._vcmd_float
                ebox = tk.Frame(body, bg=C["input_bg"], padx=4, pady=3)
                ebox.grid(row=i+2, column=2, columnspan=2,
                          sticky="w", pady=4)
                tk.Entry(ebox, textvariable=var,
                         font=("Consolas", 12, "bold"),
                         bg=C["input_bg"], fg="white",
                         insertbackground="white", bd=0, width=12,
                         validate="key", validatecommand=_vcmd).pack()

        info_lbl = tk.Label(body, text="", font=self.f_small,
                            bg=C["row_alt"], fg=C["accent"],
                            wraplength=360, justify="left", pady=4)
        info_lbl.grid(row=len(rows)+2, column=0, columnspan=4,
                      sticky="ew", pady=4)

        def update_calc(*a):
            try:
                vol   = float(fields["vol"].get())
                t_sec = float(fields["time"].get()) if fields["time"].get() else 0
                tube  = fields["tube"].get()
                max_flow = TUBE_DATA[tube]["max_flow"]
                if t_sec > 0 and vol > 0:
                    flow_needed = (vol / t_sec) * 60.0
                    rpm_needed  = (flow_needed / max_flow) * MAX_RPM
                    if rpm_needed > MAX_RPM:
                        min_t = (vol / max_flow) * 60.0
                        info_lbl.config(
                            text=f"⚠ NOT POSSIBLE — min time at 350 RPM = {min_t:.1f} s",
                            fg=C["red"], bg=C["row_alt"])
                    elif rpm_needed < 1:
                        info_lbl.config(
                            text=f"⚠ Time too long — RPM < 1, reduce time",
                            fg=C["orange"], bg=C["row_alt"])
                    else:
                        flow_actual = calc_flow_rate(tube, rpm_needed)
                        info_lbl.config(
                            text=f"✔ Need {rpm_needed:.1f} RPM  |  Flow: {flow_actual:.3f} mL/min  |  {tube}",
                            fg=C["green"], bg=C["row_alt"])
                elif vol > 0:
                    info_lbl.config(text="Enter Disp. Time to calculate RPM", fg=C["text_dim"], bg=C["row_alt"])
            except: pass

        for k in ["vol", "time"]:
            fields[k].trace_add("write", update_calc)

        def save():
            try:
                rec = {
                    "channel":  ch_var.get(),
                    "tube":     fields["tube"].get(),
                    "vol":      float(fields["vol"].get()),
                    "time":     float(fields["time"].get()) if fields["time"].get() else 0.0,
                    "pause":    float(fields["pause"].get()),
                    "repeat":   int(fields["repeat"].get()) if fields["repeat"].get() else 1,
                    "speed":    float(fields["speed"].get()),
                    "suckback": float(fields["suckback"].get()) if fields["suckback"].get() else 0.0,
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
            messagebox.showinfo("No Selection", "Please select a recipe row to delete.")
            return
        idx = self._recipe_tree.index(sel[0])
        r   = self._recipes[idx]
        name = f"Pump {r.get('channel',1)} — Vol: {r.get('vol',0):.1f} mL"
        if not messagebox.askyesno("Delete Recipe",
                "Delete this recipe?\n\n" + name + "\n\nThis cannot be undone."):
            return
        self._recipes.pop(idx)
        self._settings["recipes"] = self._recipes
        save_settings(self._settings)
        self._refresh_recipe_tree()

    def _clear_recipes(self):
        count = len(self._recipes)
        if count == 0:
            messagebox.showinfo("Nothing to Clear", "There are no recipes to clear.")
            return
        if messagebox.askyesno(
                "Confirm Clear",
                f"Are you sure you want to clear all {count} recipe(s)?\n\n"
                "This will permanently delete all saved programs.\n"
                "This action cannot be undone.",
                icon="warning"):
            self._recipes.clear()
            self._settings["recipes"] = self._recipes
            save_settings(self._settings)
            self._refresh_recipe_tree()

    def _on_recipe_row_click(self, event=None):
        """Apply selected recipe settings to the correct pump Dispensing page.
        The user then goes to Dispensing tab and runs from there — no direct motor control here."""
        sel = self._recipe_tree.selection()
        if not sel:
            messagebox.showinfo("No Selection",
                "Please select a recipe row first, then click APPLY TO DISPENSING.")
            return
        idx_row = self._recipe_tree.index(sel[0])
        if idx_row >= len(self._recipes):
            return
        r  = self._recipes[idx_row]
        ch = r.get("channel", 1) - 1   # 0-indexed

        try:
            # Load dispensing fields
            if self._dv_vol[ch]:
                self._dv_vol[ch].set(r.get("vol", 10.0))
            if self._dv_time[ch]:
                self._dv_time[ch].set(float(r.get("time", 0.0)))
            if self._dv_pause[ch]:
                self._dv_pause[ch].set(r.get("pause", 1.0))
            if self._dv_rep[ch]:
                self._dv_rep[ch].set(r.get("repeat", 1))
            # Apply tube + suckback (propagates to all pages)
            if self._tube_var[ch]:
                self._tube_var[ch].set(r.get("tube", "2x1mm"))
                self._on_tube_change(ch)
            if self._suckback_var[ch]:
                self._suckback_var[ch].set(r.get("suckback", 0.0))
            # Update dashboard RPM reference
            if self._rpm_var[ch]:
                self._rpm_var[ch].set(int(r.get("speed", 60)))
            self._update_calc_time(ch)

            # Switch to Dispensing tab so user can review and click START
            self._nb.select(1)
        except Exception as e:
            messagebox.showerror("Error", str(e))


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

        self._tube_info_lbl = getattr(self, "_tube_info_lbl", [None, None])
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
            # Auto-save when suckback changes so setting persists on restart
            self._suckback_var[i].trace_add("write",
                lambda *a, ch=i: self._settings.update({
                    f"suckback{ch+1}": self._suckback_var[ch].get()
                }) or save_settings(self._settings))

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
        if getattr(self, "_connecting", False):
            return   # already in progress — ignore double-click
        self._connecting = True
        port = self._port_var.get().strip()
        if not port:
            messagebox.showwarning("No Port", "Enter a COM port name first (e.g. COM3 or /dev/ttyUSB0).")
            self._connecting = False
            return
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
                # On connect, always send STOP to guarantee known state.
                # If the pump was physically running before connecting, this
                # brings it to a safe stopped state immediately.
                try: self.pump1.stop()
                except: pass
                try: self.pump2.stop()
                except: pass
                self._start_watchdog()
            else:
                self.pump1 = None
                self.pump2 = None

            def update_ui():
                self._connecting = False
                if ok:
                    self._conn_lbl.config(text="● CONNECTED", fg="#69F0AE")
                    self._update_tube_labels()
                else:
                    self._conn_lbl.config(text="● DISCONNECTED", fg="#FF8A80")
                    messagebox.showerror("Connection Failed",
                        "Could not open " + port + ".\n\n"
                        "Check:\n"
                        "  \u2022 Correct COM port selected\n"
                        "  \u2022 USB adapter plugged in\n"
                        "  \u2022 Driver installed (CP210x / CH340)\n"
                        "  \u2022 No other program using the port")

            self.after(0, update_ui)

        threading.Thread(target=connect, daemon=True).start()

    def _start_watchdog(self):
        # Cancel any previous watchdog
        ev = getattr(self, "_watchdog_stop", None)
        if ev:
            ev.set()
        stop_ev = threading.Event()
        self._watchdog_stop = stop_ev

        def watch():
            while not stop_ev.is_set():
                time.sleep(2.0)
                if stop_ev.is_set():
                    break
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
        # Stop watchdog FIRST so it doesn't fire _on_cable_removed
        ev = getattr(self, "_watchdog_stop", None)
        if ev:
            ev.set()
        self._watchdog_stop = None
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

    def _update_calc_time(self, idx):
        """
        Core calculation: Vol + Disp.Time → Required RPM.
        Debounced: cancels previous scheduled call so rapid typing/slider
        doesn't flood the UI with redundant label updates.
        """
        debounce_key = f"_calc_pending_{idx}"
        old_job = getattr(self, debounce_key, None)
        if old_job:
            try: self.after_cancel(old_job)
            except: pass
        job = self.after(120, lambda: self._do_update_calc_time(idx))
        setattr(self, debounce_key, job)

    def _do_update_calc_time(self, idx):
        """Actual calculation — called after debounce delay."""
        try:
            vol   = self._dv_vol[idx].get()
            t_sec = self._dv_time[idx].get() if self._dv_time[idx] else 0
            tube  = self._tube_var[idx].get()
            calib = self._calib_factor[idx].get()
            max_flow = TUBE_DATA[tube]["max_flow"]  # mL/min at 350 RPM

            if vol <= 0:
                return

            if t_sec > 0:
                # ── Main mode: Vol + Time → Required RPM ─────────────────
                flow_needed = (vol / t_sec) * 60.0          # mL/min
                rpm_needed  = (flow_needed / max_flow) * MAX_RPM

                if rpm_needed > MAX_RPM:
                    # Physically impossible with this tube
                    min_time = (vol / max_flow) * 60.0      # s at 350 RPM
                    msg = ("⚠  NOT POSSIBLE  |  Vol: {:.2f} mL  |  Time: {:.1f} s  |  "
                           "Min time at 350 RPM = {:.1f} s  |  "
                           "Reduce volume OR increase time").format(vol, t_sec, min_time)
                    clr = C["red"]
                    rpm_display = "---"
                elif rpm_needed < 1:
                    msg = ("⚠  Time too long  |  Vol: {:.2f} mL  |  Time: {:.1f} s  |  "
                           "Required RPM < 1 — reduce time or increase volume").format(vol, t_sec)
                    clr = C["orange"]
                    rpm_display = "< 1"
                else:
                    # actual flow = theoretical / calib (pump under-delivers when calib>1)
                    flow_theoretical = calc_flow_rate(tube, rpm_needed)
                    flow_actual = flow_theoretical / calib if calib > 0 else flow_theoretical
                    run_t_with_calib = t_sec * calib
                    msg = ("✔  Vol: {:.2f} mL  |  Time: {:.1f} s  →  "
                           "Need {:.1f} RPM  |  Actual flow: {:.3f} mL/min  |  "
                           "Motor runs: {:.1f} s (calib ×{:.3f})").format(
                               vol, t_sec, rpm_needed, flow_actual, run_t_with_calib, calib)
                    clr = C["green"]
                    rpm_display = "{:.0f}".format(rpm_needed)
            else:
                # ── Disp. Time = 0 → prompt the user to enter a time ────────
                msg = "⚠  Please enter Disp. Time (seconds) to calculate required RPM"
                clr = C["orange"]
                rpm_display = "---"

            if hasattr(self, "_calc_time_lbl") and self._calc_time_lbl[idx]:
                self._calc_time_lbl[idx].config(text=msg, fg=clr)
            if hasattr(self, "_disp_rpm_display") and self._disp_rpm_display[idx]:
                # Color the big RPM number:
                #   teal   = Vol+Time calculation is valid and achievable
                #   yellow = RPM too low (< 1) or Disp.Time not set
                #   red    = physically impossible (> 350 RPM)
                if clr == C["green"]:
                    rpm_clr = "#64FFDA"     # teal   — achievable
                elif clr == C["orange"]:
                    rpm_clr = "#FFB74D"     # yellow — RPM < 1 or time missing
                else:
                    rpm_clr = "#FF5252"     # red    — impossible
                self._disp_rpm_display[idx].config(text=rpm_display, fg=rpm_clr)
        except Exception:
            pass

    def _get_required_rpm(self, idx):
        """Return (rpm, raw_time) needed to deliver _dv_vol in _dv_time seconds.
        Returns (None, None) if impossible or if Disp.Time = 0 (not set)."""
        try:
            vol   = self._dv_vol[idx].get()
            t_sec = self._dv_time[idx].get() if self._dv_time[idx] else 0
            tube  = self._tube_var[idx].get()
            calib = self._calib_factor[idx].get()
            max_flow = TUBE_DATA[tube]["max_flow"]

            if t_sec > 0 and vol > 0:
                flow_needed = (vol / t_sec) * 60.0
                rpm_needed  = (flow_needed / max_flow) * MAX_RPM
                if 1 <= rpm_needed <= MAX_RPM:
                    return round(rpm_needed, 1), t_sec  # return RAW time; calib applied in caller
                return None, None   # impossible
            else:
                # t_sec = 0 — user has not set a Disp. Time; block START
                return None, None
        except Exception:
            return None, None

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
        # Update dashboard labels (flow, tube name, RPM)
        self._update_tube_labels()
        # Update settings panel tube info label
        if hasattr(self, "_tube_info_lbl") and self._tube_info_lbl[idx]:
            self._tube_info_lbl[idx].config(
                text=f"Max: {data.get('max_flow',0):.2f} mL/min @ 350 RPM")
        # Update dispensing tab — recalculate Required RPM with new tube
        self._update_calc_time(idx)
        # Update live tracking so volume uses correct tube flow rate
        self._update_dashboard_tracking_state(idx)
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
        # Cancel any pending debounced RPM write before snapshotting
        _pending = getattr(self, f"_rpm_pending_{idx}", None)
        if _pending:
            try: self.after_cancel(_pending)
            except: pass
            setattr(self, f"_rpm_pending_{idx}", None)
        # Snapshot Tkinter vars before thread
        _snap_rpm = int(self._rpm_var[idx].get()) if self._rpm_var[idx] else 60
        _snap_fwd = (self._global_direction[idx].get() == "CW")

        def run():
            try:
                rpm = _snap_rpm
                fwd = _snap_fwd
                pump.set_speed(rpm)
                pump.set_direction(fwd)
                pump.start()
                self.after(0, lambda i=idx: self._update_motor_ui(i))
                self.after(100, lambda i=idx, r=rpm: self._start_dashboard_volume_tracking(i, r))
            except Exception:
                self._motor_locked_by[idx] = None
                self.after(0, lambda i=idx: self._update_motor_ui(i))
        threading.Thread(target=run, daemon=True).start()

    def _cmd_stop(self, idx):
        """Dashboard STOP = global emergency stop.
        Kills dispensing thread, timing thread, volume tracking,
        and sends hardware stop — regardless of which page owns the motor."""
        pump = self._get_pump(idx)

        # 1. Kill dispense thread if running (sets stop_ev, stops hardware)
        self._stop_dispense(idx)

        # 2. Cancel any active timer
        self._cancel_timing(idx, stop_motor=False)  # motor stop handled below

        # 3. Stop dashboard volume tracking
        self._stop_dashboard_volume_tracking(idx)

        # 4. Release motor lock
        self._motor_locked_by[idx] = None

        # 5. Send hardware stop in thread (non-blocking)
        def force_stop():
            if pump:
                pump.stop()
            self.after(0, lambda i=idx: self._update_motor_ui(i))
        threading.Thread(target=force_stop, daemon=True).start()

    def _set_rpm(self, idx, rpm):
        rpm = int(rpm)
        self._rpm_var[idx].set(rpm)
        # Always update UI labels
        tube = self._tube_var[idx].get()
        flow = calc_flow_rate(tube, rpm)
        if hasattr(self, "_flow_lbl") and self._flow_lbl[idx]:
            self._flow_lbl[idx].config(text=f"{flow:.3f} mL/min")
        if hasattr(self, "_rpm_lbl") and self._rpm_lbl[idx]:
            self._rpm_lbl[idx].config(text=f"{rpm} RPM")
        self._update_calc_time(idx)
        self._update_dashboard_tracking_state(idx)
        # Block MODBUS write if motor is locked by another page
        if self._motor_locked_by[idx] in ("dispensing", "calibration", "timing"):
            return
        pump = self._get_pump(idx)
        if pump and pump.is_connected():
            def send(r=rpm, p=pump):
                try: p.set_speed(r)
                except: pass
            threading.Thread(target=send, daemon=True).start()

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
        # Always update UI labels and dispensing tab calc — these are always safe
        tube = self._tube_var[idx].get()
        flow = calc_flow_rate(tube, rpm)
        if hasattr(self, "_flow_lbl") and self._flow_lbl[idx]:
            self._flow_lbl[idx].config(text=f"{flow:.3f} mL/min")
        if hasattr(self, "_rpm_lbl") and self._rpm_lbl[idx]:
            self._rpm_lbl[idx].config(text=f"{rpm} RPM")
        self._update_calc_time(idx)

        # Block MODBUS write if motor is locked by dispensing, calibration or timing.
        # Dashboard slider must not interfere with a running dispense cycle.
        locked = self._motor_locked_by[idx]
        if locked in ("dispensing", "calibration", "timing"):
            # Still update live tracking state (harmless — dispense ignores dashboard RPM)
            self._update_dashboard_tracking_state(idx)
            return   # DO NOT send speed to pump

        # Debounce MODBUS write: cancel pending send, schedule new one 80ms later.
        pending_key = f"_rpm_pending_{idx}"
        old_job = getattr(self, pending_key, None)
        if old_job:
            try: self.after_cancel(old_job)
            except: pass
        if pump and pump.is_connected():
            def send_rpm(r=rpm, p=pump):
                try: p.set_speed(r)
                except: pass
            job = self.after(80, lambda fn=send_rpm: threading.Thread(
                target=fn, daemon=True).start())
            setattr(self, pending_key, job)
        # Update live tracking state so volume recalculates at new RPM immediately
        self._update_dashboard_tracking_state(idx)

    def _set_direction(self, idx):
        d = self._dir_var[idx].get()
        self._global_direction[idx].set(d)
        # Update direction badge always
        if hasattr(self, "_dir_lbl") and self._dir_lbl[idx]:
            self._dir_lbl[idx].config(text=d,
                bg=C["green"] if d == "CW" else C["orange"])
        # Block MODBUS write if motor is locked by another page
        if self._motor_locked_by[idx] in ("dispensing", "calibration", "timing"):
            return
        pump = self._get_pump(idx)
        if pump and pump.is_connected():
            fwd = (d == "CW")
            threading.Thread(target=lambda f=fwd: pump.set_direction(f),
                             daemon=True).start()

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

        try:
            vol    = float(self._dv_vol[idx].get())
            pause  = float(self._dv_pause[idx].get())
            repeat = int(self._dv_rep[idx].get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid Input", "Volume, pause and repeat must be valid numbers.")
            self._motor_locked_by[idx] = None
            return
        if vol <= 0:
            messagebox.showerror("Invalid Input", "Dispense volume must be greater than 0.")
            self._motor_locked_by[idx] = None
            return
        if repeat < 1:
            messagebox.showerror("Invalid Input", "Repeat count must be at least 1.")
            self._motor_locked_by[idx] = None
            return
        if pause < 0:
            pause = 0.0

        # Cancel any pending debounced RPM write — if the user dragged the
        # dashboard slider and then switched to Dispensing and clicked START,
        # the pending after(80ms) MODBUS write would fire AFTER the dispense
        # thread sets its calculated speed, overwriting it with the old value.
        _pending_rpm = getattr(self, f"_rpm_pending_{idx}", None)
        if _pending_rpm:
            try: self.after_cancel(_pending_rpm)
            except: pass
            setattr(self, f"_rpm_pending_{idx}", None)

        # Snapshot ALL Tkinter vars in UI thread before spawning background thread
        tube      = self._tube_var[idx].get()
        calib     = self._calib_factor[idx].get()
        snap_sb   = float(self._suckback_var[idx].get())   # suck-back angle degrees
        snap_fwd  = (self._global_direction[idx].get() == "CW")

        # ── Vol + Disp.Time → Required RPM ──────────────────────────────────
        # Returns (None, None) if time=0 or impossible — START is blocked.
        speed, run_time_override = self._get_required_rpm(idx)

        if speed is None:
            t_sec    = self._dv_time[idx].get() if self._dv_time[idx] else 0
            max_flow = TUBE_DATA[tube]["max_flow"]
            if t_sec <= 0:
                # User left Disp. Time at 0 — prompt them to set it
                min_time = (vol / max_flow) * 60.0
                messagebox.showwarning(
                    "Disp. Time Required",
                    f"Please enter a Disp. Time (seconds) before starting.\n\n"
                    f"For {vol:.2f} mL with tube {tube}:\n"
                    f"  • Minimum time at 350 RPM = {min_time:.1f} s\n"
                    f"  • Example: enter {round(min_time * 1.5):.0f} s for a comfortable speed"
                )
            else:
                # Physically impossible combination
                min_time = (vol / max_flow) * 60.0
                messagebox.showerror(
                    "Not Possible",
                    f"Cannot dispense {vol:.2f} mL in {t_sec:.1f} s with tube {tube}.\n\n"
                    f"Minimum possible time at 350 RPM = {min_time:.1f} s\n\n"
                    f"Options:\n"
                    f"  • Increase Disp. Time to at least {min_time:.1f} s\n"
                    f"  • Reduce volume below {max_flow * (t_sec/60):.2f} mL\n"
                    f"  • Use a larger tube  (4x1mm max = {TUBE_DATA['4x1mm']['max_flow']:.1f} mL/min)"
                )
            self._motor_locked_by[idx] = None
            return

        speed = max(1, min(350, int(round(speed))))

        # Create new stop event
        stop_ev = threading.Event()
        self._stop_events[idx] = stop_ev

        def run():
            _user_stopped = [False]   # tracks if STOP was pressed vs natural completion
            try:
                pump.stop()
            except: pass
            time.sleep(0.05)

            try:
              for cycle in range(repeat):
                if stop_ev.is_set():
                    break

                # Use snapshotted values — Tkinter vars must not be read from threads
                current_tube  = tube        # snapshotted above before thread start
                current_calib = calib       # snapshotted above before thread start
                current_speed = speed       # pre-calculated (Vol+Time→RPM)
                # Apply calibration factor to run time (corrects for pump delivery error)
                if run_time_override is not None and run_time_override > 0:
                    current_run_t = run_time_override * current_calib
                else:
                    current_run_t = calc_run_time(current_tube, current_speed, vol) * current_calib

                self.after(0, lambda c=cycle, t=current_tube: (
                    self._disp_counter[idx].config(text=f"{c+1} / {repeat}"),
                    self._disp_status[idx].config(
                        text=f"Dispensing {vol:.2f} mL  [{t}]...")
                ))

                # Use snapshotted direction — safe from thread
                actual_fwd = snap_fwd
                pump.set_speed(current_speed)
                pump.set_direction(actual_fwd)
                pump.start()
                self.after(0, lambda i=idx: self._update_motor_ui(i))

                start = time.time()
                while True:
                    if stop_ev.is_set():
                        break
                    elapsed = time.time() - start
                    if elapsed >= current_run_t:
                        break
                    pct       = min((elapsed / current_run_t) * 100, 100) if current_run_t > 0 else 100
                    # Linear interpolation: show vol * fraction_done
                    # This correctly tracks calibrated delivery — at t=current_run_t, exactly vol mL dispensed
                    dispensed = round(vol * (elapsed / current_run_t), 3) if current_run_t > 0 else 0.0
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

                # Suck-back — use snapshotted value (no Tkinter access in thread)
                sb_angle = snap_sb
                if sb_angle > 0 and current_speed > 0:
                    sb_time = (sb_angle / 360.0) * (60.0 / current_speed)
                    pump.set_direction(not actual_fwd)
                    pump.start()
                    time.sleep(sb_time)
                    pump.stop()
                    pump.set_direction(actual_fwd)

                self.after(0, lambda i=idx: self._update_motor_ui(i))

                self._total_vol[idx] += vol
                self.after(0, lambda v=self._total_vol[idx]: (
                    self._disp_volume[idx].set(round(v, 3))
                ))

                if cycle < repeat - 1 and not stop_ev.is_set():
                    self.after(0, lambda i=idx, p=pause: self._disp_status[i].config(
                        text=f"Pausing {p:.1f} s..."))
                    time.sleep(pause)

            except Exception as exc:
                # Any unexpected error — stop motor and release lock safely
                try: pump.stop()
                except: pass
            finally:
                _user_stopped[0] = stop_ev.is_set()   # True if user clicked STOP
                stop_ev.set()
                self._motor_locked_by[idx] = None

            # was_stopped is set INSIDE the finally block before stop_ev.set()
            disp_txt = f"Complete — {repeat} cycle(s) done." if not _user_stopped[0] else "Stopped by user."
            self.after(0, lambda txt=disp_txt, r=repeat, i=idx: (
                self._disp_prog[i].configure(value=0),
                self._disp_status[i].config(text=txt),
                self._disp_counter[i].config(text=f"{r} / {r}")
            ))
            self.after(0, lambda i=idx: self._update_motor_ui(i))

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
        self.after(0, lambda i=idx: self._update_motor_ui(i))

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------
    def _start_clock(self):
        def tick():
            try:
                self._clock_lbl.config(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
                self.after(1000, tick)
            except Exception:
                pass   # window destroyed — stop silently
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

        # Use a shared mutable list so UI thread can update RPM/tube/calib
        # while the tracking thread reads the latest values each tick.
        # List is safe to read/write under Python's GIL for simple assignments.
        try:
            init_rpm   = int(self._rpm_var[idx].get()) if self._rpm_var[idx] else 60
            init_tube  = self._tube_var[idx].get()
            init_calib = self._calib_factor[idx].get()
        except Exception:
            init_rpm, init_tube, init_calib = 60, "2x1mm", 1.0

        # [rpm, tube, calib] — updated by UI thread via _update_dashboard_tracking_state
        live_state = [init_rpm, init_tube, init_calib]
        state_key  = "_dash_live_state_" + str(idx)
        setattr(self, state_key, live_state)

        def track():
            total_vol = 0.0
            last_t    = time.time()
            while not stop_ev.is_set():
                if not pump or not pump.is_running:
                    break
                now   = time.time()
                dt    = now - last_t
                last_t = now

                # Read live state — updated by UI thread when RPM/tube/calib changes
                cur_rpm, cur_tube, cur_calib = live_state[0], live_state[1], live_state[2]

                flow_theoretical = calc_flow_rate(cur_tube, cur_rpm)
                flow       = flow_theoretical / cur_calib if cur_calib > 0 else flow_theoretical
                total_vol += flow * (dt / 60.0)
                total_vol  = round(total_vol, 3)

                self.after(0, lambda v=total_vol, i=idx: self._disp_volume[i].set(v))
                time.sleep(0.2)

        threading.Thread(target=track, daemon=True).start()

    def _update_dashboard_tracking_state(self, idx):
        """Called from UI thread when RPM/tube/calib changes while motor is running.
        Updates the live_state list so tracking thread uses current values."""
        state_key = "_dash_live_state_" + str(idx)
        live_state = getattr(self, state_key, None)
        if live_state is None:
            return
        try:
            live_state[0] = int(self._rpm_var[idx].get()) if self._rpm_var[idx] else 60
            live_state[1] = self._tube_var[idx].get()
            live_state[2] = self._calib_factor[idx].get()
        except Exception:
            pass

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
            threading.Thread(target=lambda f=fwd: pump.set_direction(f),
                             daemon=True).start()
        if hasattr(self, "_dir_lbl") and self._dir_lbl[idx]:
            self._dir_lbl[idx].config(text=d,
                bg=C["green"] if d == "CW" else C["orange"])

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
            "dir1":      self._global_direction[0].get(),
            "dir2":      self._global_direction[1].get(),
        })
        save_settings(self._settings)
        messagebox.showinfo("Saved", "Settings saved successfully!")

    def _on_close(self):
        for idx in range(2):
            self._stop_dispense(idx)
        self._disconnect_all()
        # Save silently — no messagebox popup on exit
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
            "dir1":      self._global_direction[0].get(),
            "dir2":      self._global_direction[1].get(),
        })
        save_settings(self._settings)
        self.destroy()


# ============================================================================
if __name__ == "__main__":
    # ── Dependency check before launching UI ─────────────────────────────────
    missing = []
    try:
        import pymodbus
    except ImportError:
        missing.append("pymodbus  →  pip install pymodbus")
    try:
        import serial
    except ImportError:
        missing.append("pyserial  →  pip install pyserial")

    if missing:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror("Missing Dependencies",
            "The following packages are required but not installed:\n\n" +
            "\n".join(missing) +
            "\n\nInstall them and restart.")
        _r.destroy()
        sys.exit(1)

    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = PumpHMI()
    app.mainloop()