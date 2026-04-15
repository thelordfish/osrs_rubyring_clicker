"""
Clicker 2 — A/O/B/C/D Sequence Clicker
=====================================
Positions set manually by hovering. Timing extracted from recording.
Movement between positions uses recorded human movement templates.
Pointer acceleration auto-calibrated at startup.

pip install pynput numpy scipy
python clicker2.py
"""

import ctypes
import ctypes.wintypes as wt
import math
import sys, time, json, threading
from pathlib import Path
import numpy as np
from scipy.stats import lognorm

if sys.platform != "win32":
    sys.exit("Windows only.")

ctypes.windll.user32.SetProcessDPIAware()

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURABLE
# ═══════════════════════════════════════════════════════════════════════════════

NUDGE_CHANCE          = 0.05
NUDGE_MIN_PX          = 1
NUDGE_MAX_PX          = 3
MOVE_TIME_SCALE       = 1.0
MOVE_MAX_TEMPLATE_DUR = 1.0
MOVE_LAND_SPREAD      = 4
SETTLE_TIME           = 0.05
MICRO_MOVE_CHANCE     = 0.15
MICRO_MOVE_RADIUS     = 5
A_DIST_THRESH         = 100
MIN_BURST_CLICKS      = 2
MIN_INTERVAL          = 150
INTERVAL_BUFFER_MIN   = 1.0
INTERVAL_BUFFER_MAX   = 4.0

# ═══════════════════════════════════════════════════════════════════════════════
# WIN32 CONSTANTS AND TYPES
# ═══════════════════════════════════════════════════════════════════════════════

WM_INPUT        = 0x00FF
WM_QUIT         = 0x0012
RID_INPUT       = 0x10000003
RIDEV_INPUTSINK = 0x00000100
RIM_TYPEMOUSE   = 0
RI_LEFT_DOWN    = 0x0001
RI_LEFT_UP      = 0x0002
INPUT_MOUSE     = 0
EF_MOVE         = 0x0001
EF_LEFTDOWN     = 0x0002
EF_LEFTUP       = 0x0004
HWND_MESSAGE    = wt.HWND(-3)

ULONG_PTR = ctypes.c_uint64 if sys.maxsize > 2**32 else ctypes.c_uint32
LRESULT   = ctypes.c_int64  if sys.maxsize > 2**32 else ctypes.c_int32
WPARAM    = ctypes.c_uint64 if sys.maxsize > 2**32 else ctypes.c_uint32
LPARAM    = ctypes.c_int64  if sys.maxsize > 2**32 else ctypes.c_int32

user32   = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.restype  = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, WPARAM, LPARAM]
user32.SetCursorPos.argtypes   = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype    = wt.BOOL

class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]

class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]

class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]

class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wt.USHORT), ("ulButtons", wt.ULONG),
                ("ulRawButtons", wt.ULONG), ("lLastX", wt.LONG),
                ("lLastY", wt.LONG), ("ulExtraInformation", wt.ULONG)]

class _RAWINPUT_DATA(ctypes.Union):
    _fields_ = [("mouse", RAWMOUSE)]

class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWINPUT_DATA)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("_input", _INPUT_UNION)]

WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, WPARAM, LPARAM)

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROCTYPE),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

# ═══════════════════════════════════════════════════════════════════════════════
# LOW-LEVEL INPUT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return [int(pt.x), int(pt.y)]

def _set_cursor_pos(x, y):
    """Direct SetCursorPos — calibration only, never during playback."""
    user32.SetCursorPos(int(round(x)), int(round(y)))

def _build_mouse_input(flags, dx=0, dy=0):
    inp = INPUT()
    inp.type              = INPUT_MOUSE
    inp._input.mi.dx      = dx
    inp._input.mi.dy      = dy
    inp._input.mi.dwFlags = flags
    return inp

def _send_inputs(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

def _send_relative_move(dx, dy):
    _send_inputs(_build_mouse_input(EF_MOVE, dx=dx, dy=dy))

def _send_click_down():
    _send_inputs(_build_mouse_input(EF_LEFTDOWN))

def _send_click_up():
    _send_inputs(_build_mouse_input(EF_LEFTUP))

def _spin_wait(duration):
    deadline = time.perf_counter() + duration
    while time.perf_counter() < deadline:
        pass

# ═══════════════════════════════════════════════════════════════════════════════
# POINTER ACCELERATION CALIBRATION
# ═══════════════════════════════════════════════════════════════════════════════

_calib_raw_deltas    = np.array([0.0])
_calib_actual_pixels = np.array([0.0])

def _run_calibration():
    global _calib_raw_deltas, _calib_actual_pixels
    print("  Take hand off mouse...")
    for i in range(3, 0, -1):
        print(f"    {i}...")
        time.sleep(1)
    print("    Calibrating...")

    test_deltas = [1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 80, 100, 150, 200]
    screen_cx = user32.GetSystemMetrics(0) // 2
    screen_cy = user32.GetSystemMetrics(1) // 2
    original_pos = _get_cursor_pos()

    raw_deltas    = [0.0]
    actual_pixels = [0.0]

    for delta in test_deltas:
        _set_cursor_pos(screen_cx, screen_cy)
        time.sleep(0.03)
        x_before, _ = _get_cursor_pos()
        _send_relative_move(delta, 0)
        time.sleep(0.03)
        x_after, _ = _get_cursor_pos()
        displacement = x_after - x_before
        if displacement > 0:
            raw_deltas.append(float(delta))
            actual_pixels.append(float(displacement))

    _set_cursor_pos(original_pos[0], original_pos[1])
    _calib_raw_deltas    = np.array(raw_deltas)
    _calib_actual_pixels = np.array(actual_pixels)

    if len(raw_deltas) >= 3:
        ratios = [a / d for d, a in zip(raw_deltas[1:], actual_pixels[1:]) if d > 0]
        spread = max(ratios) - min(ratios)
        if spread < 0.1:
            print(f"  Calibration: flat ratio {ratios[0]:.2f}x — no acceleration.")
        else:
            print(f"  Calibration: ratio {min(ratios):.2f}x – {max(ratios):.2f}x — acceleration active.")
    else:
        print("  Calibration: insufficient data, corrections disabled.")


def _get_accel_correction(desired_pixels):
    if desired_pixels < 0.5 or len(_calib_actual_pixels) < 3:
        return 1.0
    raw_needed = float(np.interp(desired_pixels, _calib_actual_pixels, _calib_raw_deltas))
    return raw_needed / desired_pixels

# ═══════════════════════════════════════════════════════════════════════════════
# HUMAN MOUSE MOVEMENT
# ═══════════════════════════════════════════════════════════════════════════════

_move_templates = []

def _load_move_templates():
    if not TEMPLATES_FILE.exists():
        print(f"  WARNING: {TEMPLATES_FILE.name} not found. Run extract_templates.py first.")
        return []
    all_templates = json.loads(TEMPLATES_FILE.read_text())
    pool = [t for t in all_templates if t["orig_dur"] <= MOVE_MAX_TEMPLATE_DUR]
    if not pool:
        pool = all_templates
    print(f"  Loaded {len(pool)} movement templates (from {len(all_templates)} total).")
    return pool


def _send_corrected_move(pixel_dx, pixel_dy):
    magnitude = math.sqrt(pixel_dx * pixel_dx + pixel_dy * pixel_dy)
    correction = _get_accel_correction(magnitude)
    raw_dx = int(round(pixel_dx * correction))
    raw_dy = int(round(pixel_dy * correction))
    if raw_dx != 0 or raw_dy != 0:
        _send_relative_move(raw_dx, raw_dy)


def _replay_template_to(target_x, target_y):
    current_x, current_y = _get_cursor_pos()
    dx = target_x - current_x
    dy = target_y - current_y
    distance = math.sqrt(dx * dx + dy * dy)

    if distance < 2:
        return

    template = _move_templates[np.random.randint(len(_move_templates))]
    angle = math.atan2(dy, dx)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)

    desired_x, desired_y = 0.0, 0.0
    sent_ix, sent_iy = 0, 0
    accumulated_dt = 0.0

    for norm_dx, norm_dy, dt in template["deltas"]:
        scaled_dx = norm_dx * distance
        scaled_dy = norm_dy * distance
        rotated_dx = scaled_dx * cos_a - scaled_dy * sin_a
        rotated_dy = scaled_dx * sin_a + scaled_dy * cos_a

        desired_x += rotated_dx
        desired_y += rotated_dy
        accumulated_dt += dt

        target_ix = int(round(desired_x))
        target_iy = int(round(desired_y))
        step_dx = target_ix - sent_ix
        step_dy = target_iy - sent_iy

        if step_dx != 0 or step_dy != 0:
            if accumulated_dt > 0:
                _spin_wait(accumulated_dt * MOVE_TIME_SCALE)
            _send_corrected_move(step_dx, step_dy)
            sent_ix, sent_iy = target_ix, target_iy
            accumulated_dt = 0.0


def _correct_position_to(target_x, target_y, max_attempts=5):
    for _ in range(max_attempts):
        actual_x, actual_y = _get_cursor_pos()
        err_x = target_x - actual_x
        err_y = target_y - actual_y
        if abs(err_x) <= 1 and abs(err_y) <= 1:
            return actual_x, actual_y
        _send_corrected_move(err_x, err_y)
        time.sleep(0.01)
    return _get_cursor_pos()


def _apply_landing_scatter():
    offset_x = np.random.randint(-MOVE_LAND_SPREAD, MOVE_LAND_SPREAD + 1)
    offset_y = np.random.randint(-MOVE_LAND_SPREAD, MOVE_LAND_SPREAD + 1)
    if offset_x != 0 or offset_y != 0:
        _send_relative_move(offset_x, offset_y)


def _move_cursor_human(target_x, target_y, precise=False):
    current_x, current_y = _get_cursor_pos()
    distance = math.sqrt((target_x - current_x)**2 + (target_y - current_y)**2)

    if distance < 2 or not _move_templates:
        if distance >= 0.5:
            _send_corrected_move(target_x - current_x, target_y - current_y)
        return

    _replay_template_to(target_x, target_y)

    if precise:
        _correct_position_to(target_x, target_y)
        _apply_landing_scatter()
    else:
        landing_x = target_x + np.random.randint(-MOVE_LAND_SPREAD, MOVE_LAND_SPREAD + 1)
        landing_y = target_y + np.random.randint(-MOVE_LAND_SPREAD, MOVE_LAND_SPREAD + 1)
        actual_x, actual_y = _get_cursor_pos()
        err_x = landing_x - actual_x
        err_y = landing_y - actual_y
        if err_x != 0 or err_y != 0:
            _send_corrected_move(err_x, err_y)


# ═══════════════════════════════════════════════════════════════════════════════
# RAW INPUT WINDOW (recording only)
# ═══════════════════════════════════════════════════════════════════════════════

_raw_events = []
_recording  = False
_replaying  = False
_hwnd       = None

def _process_raw_mouse(mouse_data):
    button_flags = mouse_data.ulButtons & 0xFFFF
    button_data  = mouse_data.ulButtons >> 16
    if button_data >= 0x8000:
        button_data -= 0x10000
    t = time.perf_counter()
    if mouse_data.lLastX != 0 or mouse_data.lLastY != 0:
        _raw_events.append({"type": "move", "dx": int(mouse_data.lLastX),
                            "dy": int(mouse_data.lLastY), "t": t})
    if button_flags:
        screen_pos = _get_cursor_pos()
        _raw_events.append({"type": "button", "flags": button_flags,
                            "data": button_data, "t": t,
                            "cx": screen_pos[0], "cy": screen_pos[1]})

def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_INPUT:
        cb = wt.UINT(0)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(cb),
                               ctypes.sizeof(RAWINPUTHEADER))
        if cb.value > 0:
            buf = (ctypes.c_ubyte * cb.value)()
            user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(cb),
                                   ctypes.sizeof(RAWINPUTHEADER))
            if _recording:
                raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                if raw.header.dwType == RIM_TYPEMOUSE:
                    _process_raw_mouse(raw.data.mouse)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

_wnd_proc_cb = WNDPROCTYPE(_wnd_proc)

def _ensure_raw_input_window():
    global _hwnd
    if _hwnd is not None:
        return
    def _run():
        global _hwnd
        hinstance  = kernel32.GetModuleHandleW(None)
        class_name = "Clicker2Raw"
        wc = WNDCLASSEXW()
        wc.cbSize        = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc   = _wnd_proc_cb
        wc.hInstance      = hinstance
        wc.lpszClassName  = class_name
        user32.RegisterClassExW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, class_name, "RawInput", 0, 0, 0, 0, 0,
                                      HWND_MESSAGE, None, hinstance, None)
        if not hwnd:
            print("ERROR: Could not create raw input window.")
            return
        _hwnd = hwnd
        rid = RAWINPUTDEVICE()
        rid.usUsagePage = 0x01
        rid.usUsage     = 0x02
        rid.dwFlags     = RIDEV_INPUTSINK
        rid.hwndTarget  = hwnd
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))
        msg = wt.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    threading.Thread(target=_run, daemon=True).start()
    time.sleep(0.2)

# ═══════════════════════════════════════════════════════════════════════════════
# CLICK AND TIMING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _sample_hold(model):
    s, loc, sc = model["o_hold_lognorm"]
    return max(0.02, lognorm.rvs(s, loc=loc, scale=sc))

def _sample_short_gap(model):
    s, loc, sc = model["o_short_gap_lognorm"]
    return max(0.05, lognorm.rvs(s, loc=loc, scale=sc))

def _sample_long_gap(model):
    s, loc, sc = model["o_long_gap_lognorm"]
    return max(0.30, lognorm.rvs(s, loc=loc, scale=sc))

def _sample_a_hold(model):
    s, loc, sc = model["a_hold_lognorm"]
    return max(0.02, lognorm.rvs(s, loc=loc, scale=sc))

def _sample_a_gap(model):
    s, loc, sc = model["a_gap_lognorm"]
    return max(0.05, lognorm.rvs(s, loc=loc, scale=sc))

def _click_once(hold_duration):
    _send_click_down()
    _spin_wait(hold_duration)
    _send_click_up()

def _click_pair_at_current_pos(model):
    nudge_dx, nudge_dy = 0, 0
    if np.random.random() < NUDGE_CHANCE:
        angle = np.random.uniform(0, 2 * np.pi)
        radius = np.random.uniform(NUDGE_MIN_PX, NUDGE_MAX_PX)
        nudge_dx = int(round(math.cos(angle) * radius))
        nudge_dy = int(round(math.sin(angle) * radius))
        if nudge_dx or nudge_dy:
            _send_relative_move(nudge_dx, nudge_dy)

    _click_once(_sample_hold(model))
    _spin_wait(_sample_short_gap(model))
    _click_once(_sample_hold(model))

    if nudge_dx or nudge_dy:
        _send_relative_move(-nudge_dx, -nudge_dy)

# ═══════════════════════════════════════════════════════════════════════════════
# SEQUENCE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def _pick_a_burst_count(model):
    counts = model["a_burst_counts"]
    base = counts[np.random.randint(len(counts))]
    return max(1, base + np.random.randint(-1, 2))


def _jittered_position(pos, spread):
    return (pos[0] + np.random.randint(-spread, spread + 1),
            pos[1] + np.random.randint(-spread, spread + 1))


def _fire_a_burst(model):
    a_target = _jittered_position(model["a_pos"], 3)
    o_pos    = model["o_pos"]
    n_clicks = _pick_a_burst_count(model)

    print(f"    A burst: {n_clicks} clicks")
    _move_cursor_human(a_target[0], a_target[1], precise=True)
    time.sleep(SETTLE_TIME)

    for i in range(n_clicks):
        _click_once(_sample_a_hold(model))
        if i < n_clicks - 1:
            _spin_wait(_sample_a_gap(model))

    _move_cursor_human(o_pos[0], o_pos[1], precise=True)
    time.sleep(SETTLE_TIME)


def _visit_positions_b_c_d(model):
    o_pos = model["o_pos"]

    _click_once(_sample_hold(model))
    _spin_wait(_sample_short_gap(model))

    for key in ("b_pos", "c_pos", "d_pos"):
        pos = model.get(key)
        if not pos:
            continue
        target = _jittered_position(pos, 2)
        _move_cursor_human(target[0], target[1], precise=True)
        time.sleep(SETTLE_TIME)
        _click_once(_sample_hold(model))
        _spin_wait(_sample_short_gap(model))

    _move_cursor_human(o_pos[0], o_pos[1], precise=True)
    time.sleep(SETTLE_TIME)


def _run_sequence(model, should_stop):
    o_pos = model["o_pos"]
    _move_cursor_human(o_pos[0], o_pos[1], precise=True)
    time.sleep(0.1)

    print("  Firing initial A burst...")
    _fire_a_burst(model)
    if should_stop(): return
    _visit_positions_b_c_d(model)
    if should_stop(): return

    cycle_index   = 1
    anchor_time   = time.perf_counter()
    interval      = model["interval_mean"]
    next_burst    = anchor_time + interval + np.random.uniform(INTERVAL_BUFFER_MIN, INTERVAL_BUFFER_MAX)
    seconds_until = next_burst - time.perf_counter()
    print(f"  O rhythm running. Next A burst in ~{seconds_until:.0f}s. Press Enter to stop.")

    while not should_stop():
        now = time.perf_counter()

        if now >= next_burst:
            elapsed = now - anchor_time
            print(f"  [{elapsed:.0f}s elapsed] Firing A burst sequence...")
            _fire_a_burst(model)
            if should_stop(): break
            _visit_positions_b_c_d(model)
            if should_stop(): break

            cycle_index  += 1
            ideal_next    = anchor_time + cycle_index * interval
            buffer        = np.random.uniform(INTERVAL_BUFFER_MIN, INTERVAL_BUFFER_MAX)
            next_burst    = ideal_next + buffer
            seconds_until = next_burst - time.perf_counter()
            print(f"  Next A burst in ~{seconds_until:.0f}s.")
            continue

        _click_pair_at_current_pos(model)
        if should_stop(): break
        _spin_wait(_sample_long_gap(model))

# ═══════════════════════════════════════════════════════════════════════════════
# STATIONARY CLICK MODE
# ═══════════════════════════════════════════════════════════════════════════════

def _stationary_click_loop(model, centre, should_stop):
    _move_cursor_human(centre[0], centre[1], precise=True)
    time.sleep(SETTLE_TIME)

    while not should_stop():
        if np.random.random() < MICRO_MOVE_CHANCE:
            offset = _jittered_position(centre, MICRO_MOVE_RADIUS)
            _move_cursor_human(offset[0], offset[1], precise=False)

        _click_pair_at_current_pos(model)
        _spin_wait(_sample_long_gap(model))

# ═══════════════════════════════════════════════════════════════════════════════
# HUMANIZED REPLAY
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_clicks_from_recording(path):
    """Return list of {x, y, t, hold} with actual screen positions.
    Uses cx/cy from button events if available (new recordings),
    falls back to delta reconstruction for old recordings."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, list):
        events, ox, oy = data, 0, 0
    else:
        events = data.get("events", [])
        ox, oy = data.get("origin", [0, 0])

    cx, cy = ox, oy
    clicks = []
    pending = None

    for ev in events:
        if ev["type"] == "move":
            cx += ev["dx"]; cy += ev["dy"]
        elif ev["type"] == "button":
            flags = ev.get("flags", 0)
            click_x = ev.get("cx", cx)
            click_y = ev.get("cy", cy)
            if flags & RI_LEFT_DOWN:
                pending = {"x": click_x, "y": click_y, "t": ev["t"]}
            elif (flags & RI_LEFT_UP) and pending:
                pending["hold"] = ev["t"] - pending["t"]
                clicks.append(pending)
                pending = None

    return clicks


def _replay_clicks_with_human_movement(clicks, jitter_fraction, should_stop):
    if len(clicks) < 2:
        return

    gaps = [clicks[i+1]["t"] - (clicks[i]["t"] + clicks[i]["hold"])
            for i in range(len(clicks) - 1)]

    for i, click in enumerate(clicks):
        if should_stop():
            break
        _move_cursor_human(click["x"], click["y"], precise=True)
        time.sleep(SETTLE_TIME)
        _click_once(max(0.02, click["hold"]))

        if i < len(clicks) - 1 and not should_stop():
            jittered_gap = gaps[i] * (1 + np.random.uniform(-jitter_fraction, jitter_fraction))
            _spin_wait(max(0.02, jittered_gap))

# ═══════════════════════════════════════════════════════════════════════════════
# FILE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

BASE_DIR       = Path(r"C:\Users\Sherb\OneDrive\Desktop\clicker\clicker")
RECORDINGS_DIR = BASE_DIR / "recordings"
MODELS_DIR     = BASE_DIR / "models"
TEMPLATES_FILE = BASE_DIR / "human_movement" / "move_templates.json"

def _ensure_dirs():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_FILE.parent.mkdir(parents=True, exist_ok=True)

def _list_json_files(directory):
    return sorted(directory.glob("*.json"))

def _next_recording_name(directory, prefix):
    existing = {f.stem for f in _list_json_files(directory)}
    i = 1
    while f"{prefix}_{i:03d}" in existing:
        i += 1
    return directory / f"{prefix}_{i:03d}.json"

def _pick_one_file(files, label):
    if not files:
        return None
    for i, f in enumerate(files, 1):
        print(f"    {i}. {f.name}")
    while True:
        raw = input(f"  {label} number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(files):
            return files[int(raw) - 1]
        print("  Invalid.")

def _pick_multiple_files(files):
    for i, f in enumerate(files, 1):
        print(f"    {i}. {f.name}")
    raw = input("  > ").strip().lower()
    if raw == "all":
        return list(files)
    try:
        indices = [int(x.strip()) for x in raw.split(",")]
        return [files[i-1] for i in indices if 1 <= i <= len(files)]
    except:
        print("  Invalid.")
        return []

def _fit_lognormal(data, fallback):
    if len(data) >= 3:
        try:
            return list(lognorm.fit(data, floc=0))
        except:
            pass
    return fallback

def _load_model(name=None):
    models = _list_json_files(MODELS_DIR)
    if not models:
        print("\n  No models found.")
        return None, None
    if name:
        for m in models:
            if m.stem == name:
                return json.loads(m.read_text()), m
    print("\n  Choose model:")
    f = _pick_one_file(models, "Model")
    if not f:
        return None, None
    return json.loads(f.read_text()), f

def _format_pos(pos):
    return f"({pos[0]:.0f}, {pos[1]:.0f})" if pos else "NOT SET"

# ═══════════════════════════════════════════════════════════════════════════════
# MENU ACTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def action_record():
    global _recording, _raw_events
    _ensure_dirs()
    _ensure_raw_input_window()
    out = _next_recording_name(RECORDINGS_DIR, "recording")
    print(f"\n  Will save to: {out.name}")
    input("  Press Enter to start recording...")
    origin      = _get_cursor_pos()
    _raw_events = []
    _recording  = True
    print(f"  Recording... press Enter to stop.")
    input()
    _recording = False
    out.write_text(json.dumps({"origin": origin, "events": _raw_events}))
    print(f"  Saved {len(_raw_events)} events  →  {out.name}")


def action_new_model():
    _ensure_dirs()
    name = input("\n  Model name (e.g. game1): ").strip()
    if not name:
        print("  Cancelled."); return
    path = MODELS_DIR / f"{name}.json"
    if path.exists():
        print(f"  {name}.json already exists."); return
    model = {
        "o_pos": None, "b_pos": None, "c_pos": None, "d_pos": None, "a_pos": None,
        "o_rel_b": None,
        "o_hold_lognorm":      [0.12, 0, 0.093],
        "o_short_gap_lognorm": [0.30, 0, 0.457],
        "o_long_gap_lognorm":  [0.40, 0, 2.469],
        "a_hold_lognorm":      [0.12, 0, 0.080],
        "a_gap_lognorm":       [0.40, 0, 0.400],
        "a_burst_counts":      [4, 7, 12],
        "interval_mean":       218.5,
        "interval_std":        0.4,
        "misclick_variants":   [],
    }
    path.write_text(json.dumps(model, indent=2))
    print(f"  Created {name}.json  →  assign positions and extract timing next.")


def _assign_position(label):
    print(f"\n  Hover over {label} and press Enter to lock position...")
    input()
    pos = _get_cursor_pos()
    print(f"  {label} locked at ({pos[0]}, {pos[1]})")
    return pos

def _action_assign(key, label, recompute_rel_b=False):
    _ensure_dirs()
    model, path = _load_model()
    if model is None: return
    model[key] = _assign_position(label)
    if recompute_rel_b and model.get("o_pos") and model.get("b_pos"):
        model["o_rel_b"] = [model["b_pos"][0] - model["o_pos"][0],
                            model["b_pos"][1] - model["o_pos"][1]]
    path.write_text(json.dumps(model, indent=2))
    print(f"  Saved  →  {path.name}")

def action_assign_o(): _action_assign("o_pos", "O", recompute_rel_b=True)
def action_assign_b(): _action_assign("b_pos", "B", recompute_rel_b=True)
def action_assign_c(): _action_assign("c_pos", "C")
def action_assign_d(): _action_assign("d_pos", "D")
def action_assign_a(): _action_assign("a_pos", "A")


def action_extract_o_timing():
    _ensure_dirs()
    recordings = _list_json_files(RECORDINGS_DIR)
    if not recordings:
        print("\n  No recordings found."); return
    print("\n  Choose recordings for O-loop timing (comma-separated, or 'all'):")
    selected = _pick_multiple_files(recordings)
    if not selected: return

    model, path = _load_model()
    if model is None: return

    all_holds, all_gaps = [], []
    for rec in selected:
        data = json.loads(rec.read_text())
        events = data if isinstance(data, list) else data.get("events", [])
        clicks, pending = [], None
        for ev in events:
            if ev["type"] != "button": continue
            flags = ev.get("flags", 0)
            if flags & RI_LEFT_DOWN:
                pending = {"t": ev["t"]}
            elif (flags & RI_LEFT_UP) and pending:
                clicks.append({"t": pending["t"], "hold": ev["t"] - pending["t"],
                               "up_t": ev["t"]})
                pending = None
        if len(clicks) >= 2:
            all_holds.extend(c["hold"] for c in clicks)
            all_gaps.extend(clicks[i+1]["t"] - clicks[i]["up_t"]
                            for i in range(len(clicks) - 1))

    if not all_gaps:
        print("  No clicks found."); return

    median_gap = float(np.median(all_gaps))
    short_gaps = [g for g in all_gaps if g <= median_gap]
    long_gaps  = [g for g in all_gaps if g > median_gap]

    model["o_hold_lognorm"]      = _fit_lognormal(all_holds, model["o_hold_lognorm"])
    model["o_short_gap_lognorm"] = _fit_lognormal(short_gaps, model["o_short_gap_lognorm"])
    model["o_long_gap_lognorm"]  = _fit_lognormal(long_gaps,  model["o_long_gap_lognorm"])

    path.write_text(json.dumps(model, indent=2))
    for label, key, samples in [("O hold",      "o_hold_lognorm",      all_holds),
                                ("O short gap", "o_short_gap_lognorm", short_gaps),
                                ("O long gap",  "o_long_gap_lognorm",  long_gaps)]:
        s, loc, sc = model[key]
        mean_ms = lognorm.mean(s, loc=loc, scale=sc) * 1000
        print(f"  {label:14s} mean={mean_ms:.0f}ms  ({len(samples)} samples)")
    print(f"  Saved  →  {path.name}")


def action_extract_interval_timing():
    _ensure_dirs()
    recordings = _list_json_files(RECORDINGS_DIR)
    if not recordings:
        print("\n  No recordings found."); return
    print("\n  Choose recordings for interval + A-burst timing (comma-separated, or 'all'):")
    selected = _pick_multiple_files(recordings)
    if not selected: return

    model, path = _load_model()
    if model is None: return

    all_intervals, all_burst_counts, all_a_holds, all_a_gaps = [], [], [], []

    for rec in selected:
        data = json.loads(rec.read_text())
        if isinstance(data, list):
            events, ox, oy = data, 0, 0
        else:
            events = data.get("events", [])
            ox, oy = data.get("origin", [0, 0])

        cx, cy = ox, oy
        clicks = []
        for ev in events:
            if ev["type"] == "move":
                cx += ev["dx"]; cy += ev["dy"]
            elif ev["type"] == "button":
                click_x = ev.get("cx", cx)
                click_y = ev.get("cy", cy)
                if ev.get("flags", 0) & RI_LEFT_DOWN:
                    clicks.append({"x": click_x, "y": click_y, "t": ev["t"]})
        if len(clicks) < 10:
            continue

        median_x = float(np.median([c["x"] for c in clicks]))
        median_y = float(np.median([c["y"] for c in clicks]))

        for c in clicks:
            c["is_a"] = math.sqrt((c["x"]-median_x)**2 + (c["y"]-median_y)**2) > A_DIST_THRESH

        bursts, current_burst = [], []
        for c in clicks:
            if c["is_a"]:
                current_burst.append(c)
            else:
                if len(current_burst) >= MIN_BURST_CLICKS:
                    bursts.append(current_burst)
                current_burst = []
        if len(current_burst) >= MIN_BURST_CLICKS:
            bursts.append(current_burst)

        if len(bursts) >= 2:
            starts = [b[0]["t"] for b in bursts]
            all_intervals.extend(s for s in
                (starts[i+1] - starts[i] for i in range(len(starts)-1))
                if s > MIN_INTERVAL)

        for b in bursts:
            all_burst_counts.append(len(b))
            if len(b) >= 2:
                all_a_gaps.extend(b[i+1]["t"] - b[i]["t"] for i in range(len(b)-1))

        # A hold timing — only for clicks at A position
        cx, cy = ox, oy
        pending_t = None
        for ev in events:
            if ev["type"] == "move":
                cx += ev["dx"]; cy += ev["dy"]
            elif ev["type"] == "button":
                flags = ev.get("flags", 0)
                click_x = ev.get("cx", cx)
                click_y = ev.get("cy", cy)
                if flags & RI_LEFT_DOWN:
                    dist = math.sqrt((click_x-median_x)**2 + (click_y-median_y)**2)
                    pending_t = ev["t"] if dist > A_DIST_THRESH else None
                elif (flags & RI_LEFT_UP) and pending_t is not None:
                    all_a_holds.append(ev["t"] - pending_t)
                    pending_t = None

    if all_intervals:
        model["interval_mean"] = float(np.mean(all_intervals))
        model["interval_std"]  = float(np.std(all_intervals))
    if all_burst_counts:
        model["a_burst_counts"] = sorted(set(all_burst_counts))
    model["a_hold_lognorm"] = _fit_lognormal(all_a_holds, model.get("a_hold_lognorm", [0.12, 0, 0.080]))
    model["a_gap_lognorm"]  = _fit_lognormal(all_a_gaps,  model.get("a_gap_lognorm",  [0.40, 0, 0.400]))

    path.write_text(json.dumps(model, indent=2))
    print(f"  Interval:       {model['interval_mean']:.1f}s ± {model['interval_std']:.1f}s  ({len(all_intervals)} samples)")
    print(f"  A burst counts: {model['a_burst_counts']}")
    print(f"  A holds:        {len(all_a_holds)} samples")
    print(f"  A gaps:         {len(all_a_gaps)} samples")
    print(f"  Saved  →  {path.name}")


def action_run():
    global _replaying
    model, path = _load_model()
    if model is None: return

    missing = [k for k in ("o_pos", "b_pos", "a_pos") if not model.get(k)]
    if missing:
        print(f"\n  Not ready — missing: {', '.join(missing)}")
        return

    print(f"\n  O: {_format_pos(model.get('o_pos'))}")
    print(f"  B: {_format_pos(model.get('b_pos'))}")
    print(f"  C: {_format_pos(model.get('c_pos'))}")
    print(f"  D: {_format_pos(model.get('d_pos'))}")
    print(f"  A: {_format_pos(model.get('a_pos'))}")
    print(f"  Interval: ~{model['interval_mean']:.0f}s  |  A counts: {model['a_burst_counts']}")
    print(f"  Templates: {len(_move_templates)}")
    input("\n  Press Enter to begin...")

    _replaying = True
    def _run():
        global _replaying
        _run_sequence(model, lambda: not _replaying)
        print("\n  Stopped.")
    threading.Thread(target=_run, daemon=True).start()
    input()
    _replaying = False


def action_stationary():
    global _replaying
    model, path = _load_model()
    if model is None: return

    print("\n  Hover over click position and press Enter to lock...")
    input()
    centre = _get_cursor_pos()
    print(f"  Locked at ({centre[0]}, {centre[1]})")
    print("  Clicking... press Enter to stop.\n")

    _replaying = True
    def _run():
        global _replaying
        _stationary_click_loop(model, centre, lambda: not _replaying)
        print("\n  Stopped.")
    threading.Thread(target=_run, daemon=True).start()
    input()
    _replaying = False


def action_humanized_replay():
    global _replaying
    _ensure_dirs()
    recordings = _list_json_files(RECORDINGS_DIR)
    if not recordings:
        print("\n  No recordings found."); return
    print("\n  Choose recording to replay:")
    rec = _pick_one_file(recordings, "Recording")
    if not rec: return

    clicks = _extract_clicks_from_recording(str(rec))
    if len(clicks) < 2:
        print(f"  Only {len(clicks)} click(s) — need at least 2."); return

    duration = clicks[-1]["t"] - clicks[0]["t"]
    gaps = [clicks[i+1]["t"] - (clicks[i]["t"] + clicks[i]["hold"]) for i in range(len(clicks)-1)]
    print(f"  {len(clicks)} clicks, {duration:.1f}s duration")
    print(f"  Gap range: {min(gaps)*1000:.0f}ms – {max(gaps)*1000:.0f}ms")

    raw = input("  Timing jitter % [default 10]: ").strip()
    jitter = float(raw) / 100 if raw else 0.10

    input("  Press Enter to begin...")

    _replaying = True
    def _run():
        global _replaying
        _replay_clicks_with_human_movement(clicks, jitter, lambda: not _replaying)
        print("\n  Replay done.")
        _replaying = False
    threading.Thread(target=_run, daemon=True).start()
    input("  Press Enter to stop early.\n")
    _replaying = False


def action_show_model():
    model, path = _load_model()
    if model is None: return
    print(f"\n  {path.stem}")
    for key, label in [("o_pos","O"), ("b_pos","B"), ("c_pos","C"), ("d_pos","D"), ("a_pos","A")]:
        print(f"  {label}:        {_format_pos(model.get(key))}")
    print(f"  Interval: {model['interval_mean']:.1f}s ± {model['interval_std']:.1f}s")
    print(f"  A bursts: {model['a_burst_counts']}")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

MENU = [
    ("Record",                          action_record),
    ("New model",                       action_new_model),
    ("Assign O position",               action_assign_o),
    ("Assign B position",               action_assign_b),
    ("Assign C position",               action_assign_c),
    ("Assign D position",               action_assign_d),
    ("Assign A position",               action_assign_a),
    ("Extract O-loop timing",           action_extract_o_timing),
    ("Extract interval + A timing",     action_extract_interval_timing),
    ("Run endless sequence",            action_run),
    ("Stationary click — endless",      action_stationary),
    ("Humanized replay",                action_humanized_replay),
    ("Show model",                      action_show_model),
]

def main():
    global _move_templates
    _ensure_dirs()
    print("\n  Calibrating pointer acceleration...")
    _run_calibration()
    _move_templates = _load_move_templates()
    print("\n  Clicker 2 — A/O/B/C/D Sequence")
    print("  ────────────────────────────────")
    while True:
        print()
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i}.  {label}")
        print("  q.  Quit\n")
        choice = input("  > ").strip().lower()
        if choice == "q":
            break
        if choice.isdigit() and 1 <= int(choice) <= len(MENU):
            print()
            try:
                MENU[int(choice) - 1][1]()
            except KeyboardInterrupt:
                print("\n  Interrupted.")
        else:
            print("  Invalid.")

if __name__ == "__main__":
    main()