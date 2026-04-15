"""
Test script to:
1. Check if Windows pointer acceleration is enabled
2. Measure actual vs intended displacement at various delta sizes
3. Determine if calibration is needed

Run on the target Windows machine.
"""
import ctypes
import ctypes.wintypes as wt
import sys, time

if sys.platform != "win32":
    sys.exit("Windows only.")

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

# ═══════════════════════════════════════════════════════════════════════════════
# Check acceleration setting
# ═══════════════════════════════════════════════════════════════════════════════

# SPI_GETMOUSE returns 3 ints: [threshold1, threshold2, acceleration]
# acceleration = 0 means "enhance pointer precision" is OFF
# acceleration = 1 means it's ON
SPI_GETMOUSE = 0x0003

mouse_params = (ctypes.c_int * 3)()
user32.SystemParametersInfoW(SPI_GETMOUSE, 0, ctypes.byref(mouse_params), 0)

print(f"Mouse params: threshold1={mouse_params[0]}, threshold2={mouse_params[1]}, acceleration={mouse_params[2]}")
if mouse_params[2] == 0:
    print("Pointer acceleration: OFF  (enhance pointer precision disabled)")
    print("Deltas map 1:1 to pixels — no calibration needed.")
else:
    print("Pointer acceleration: ON  (enhance pointer precision enabled)")
    print("Deltas will be scaled by Windows — calibration needed.")

# Also check mouse speed (1-20 slider)
SPI_GETMOUSESPEED = 0x0070
speed = ctypes.c_int()
user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(speed), 0)
print(f"Mouse speed: {speed.value}/20")

# ═══════════════════════════════════════════════════════════════════════════════
# Empirical calibration: send known deltas, measure actual movement
# ═══════════════════════════════════════════════════════════════════════════════

INPUT_MOUSE = 0
EF_MOVE = 0x0001
ULONG_PTR = ctypes.c_uint64 if sys.maxsize > 2**32 else ctypes.c_uint32

class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("_input", _INPUT_UNION)]

def get_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

def send_move(dx, dy):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp._input.mi.dx = dx
    inp._input.mi.dy = dy
    inp._input.mi.dwFlags = EF_MOVE
    arr = (INPUT * 1)(inp)
    user32.SendInput(1, arr, ctypes.sizeof(INPUT))

print("\n--- Calibration test ---")
print("Moving cursor to centre of screen first...")

# Warp to centre so we have room
sw = user32.GetSystemMetrics(0)
sh = user32.GetSystemMetrics(1)
user32.SetCursorPos(sw // 2, sh // 2)
time.sleep(0.1)

test_deltas = [1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 80, 100, 150, 200]

print(f"\n{'Sent dx':>10}  {'Actual dx':>10}  {'Actual dy':>10}  {'Ratio':>8}")
print("-" * 45)

for d in test_deltas:
    # Reset position
    user32.SetCursorPos(sw // 2, sh // 2)
    time.sleep(0.02)
    
    x0, y0 = get_pos()
    send_move(d, 0)
    time.sleep(0.02)
    x1, y1 = get_pos()
    
    actual_dx = x1 - x0
    actual_dy = y1 - y0
    ratio = actual_dx / d if d != 0 else 0
    
    print(f"{d:10d}  {actual_dx:10d}  {actual_dy:10d}  {ratio:8.3f}")

# Restore cursor
user32.SetCursorPos(sw // 2, sh // 2)

print("\nIf all ratios are 1.000, no calibration needed.")
print("If ratios vary with delta size, acceleration is active and we need a correction curve.")
