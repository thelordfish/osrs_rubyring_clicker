"""
Extract movement templates from recordings.

Reads every recording_*.json in RECORDINGS_DIR, extracts mouse movement
sequences between clicks that involve real cursor displacement, trims idle
before/after, normalises (rotate to +x axis, scale to unit distance),
and saves to TEMPLATES_FILE.

Re-run whenever you add new recordings.

Usage:
    python extract_templates.py
    python extract_templates.py --min-dist 20 --speed-thresh 4 --margin 3
"""

import json, math, argparse
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

RECORDINGS_DIR = Path(r"C:\Users\Sherb\OneDrive\Desktop\clicker\clicker2\recordings")
TEMPLATES_FILE = Path(r"C:\Users\Sherb\OneDrive\Desktop\clicker\clicker2\human_movement\move_templates.json")

# ── Constants ─────────────────────────────────────────────────────────────────

RI_LEFT_DOWN = 0x0001
RI_LEFT_UP   = 0x0002

# ── Extraction ────────────────────────────────────────────────────────────────

def load_events(path):
    """Load events from either format (flat list or {origin, events})."""
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return data
    return data.get("events", [])


def extract_transitions(events, min_dist, speed_thresh, margin):
    """
    Find all click-to-click movement transitions.

    Returns list of dicts:
        {deltas: [(dx,dy,dt),...], orig_dist, orig_dur, n_moves}
    """
    transitions = []

    in_gap = False
    gap_moves = []

    for ev in events:
        if ev["type"] == "move":
            if in_gap:
                gap_moves.append(ev)

        elif ev["type"] == "button":
            flags = ev.get("flags", 0)

            if flags & RI_LEFT_UP:
                in_gap = True
                gap_moves = []

            elif flags & RI_LEFT_DOWN:
                if in_gap and gap_moves:
                    t = _try_extract(gap_moves, min_dist, speed_thresh, margin)
                    if t is not None:
                        transitions.append(t)
                in_gap = False

    return transitions


def _try_extract(gap_moves, min_dist, speed_thresh, margin):
    """Trim, check distance, normalise one transition."""

    total_dx = sum(m["dx"] for m in gap_moves)
    total_dy = sum(m["dy"] for m in gap_moves)
    dist = math.sqrt(total_dx**2 + total_dy**2)

    if dist < min_dist:
        return None

    # Per-event instantaneous speed (pixels per event, not per second)
    speeds = [math.sqrt(m["dx"]**2 + m["dy"]**2) for m in gap_moves]

    # Find first and last event above speed threshold
    first = next((i for i, s in enumerate(speeds) if s > speed_thresh), 0)
    last  = len(speeds) - 1 - next(
        (i for i, s in enumerate(reversed(speeds)) if s > speed_thresh), 0
    )

    if last <= first:
        return None

    # Add margin to capture ramp-in and corrections
    first = max(0, first - margin)
    last  = min(len(gap_moves) - 1, last + margin)

    trimmed = gap_moves[first : last + 1]
    if len(trimmed) < 3:
        return None

    trim_dx = sum(m["dx"] for m in trimmed)
    trim_dy = sum(m["dy"] for m in trimmed)
    trim_dist = math.sqrt(trim_dx**2 + trim_dy**2)

    if trim_dist < min_dist:
        return None

    # Build delta sequence with inter-move times
    deltas = []
    for i, m in enumerate(trimmed):
        dt = m["t"] - trimmed[i - 1]["t"] if i > 0 else 0.0
        deltas.append((m["dx"], m["dy"], dt))

    total_time = sum(d[2] for d in deltas)

    # Normalise: rotate so net displacement is along +x, scale to unit distance
    angle = math.atan2(trim_dy, trim_dx)
    cos_a = math.cos(-angle)
    sin_a = math.sin(-angle)

    norm_deltas = []
    for dx, dy, dt in deltas:
        nx = (dx * cos_a - dy * sin_a) / trim_dist
        ny = (dx * sin_a + dy * cos_a) / trim_dist
        norm_deltas.append([nx, ny, dt])

    return {
        "deltas":    norm_deltas,
        "orig_dist": round(trim_dist, 1),
        "orig_dur":  round(total_time, 4),
        "n_moves":   len(norm_deltas),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract movement templates from recordings.")
    parser.add_argument("--min-dist",     type=float, default=20,
                        help="Minimum displacement (px) to count as a transition (default: 20)")
    parser.add_argument("--speed-thresh", type=float, default=4,
                        help="Per-event speed threshold for trimming idle (default: 4)")
    parser.add_argument("--margin",       type=int,   default=3,
                        help="Extra events to keep before/after active region (default: 3)")
    parser.add_argument("--recordings",   type=str,   default=None,
                        help="Override recordings directory path")
    parser.add_argument("--output",       type=str,   default=None,
                        help="Override output file path")
    args = parser.parse_args()

    rec_dir  = Path(args.recordings) if args.recordings else RECORDINGS_DIR
    out_file = Path(args.output)     if args.output     else TEMPLATES_FILE

    recordings = sorted(rec_dir.glob("recording_*.json"))
    if not recordings:
        print(f"No recordings found in {rec_dir}")
        return

    print(f"Scanning {len(recordings)} recording(s) in {rec_dir}\n")

    all_templates = []

    for rec_path in recordings:
        events = load_events(rec_path)
        if not events:
            print(f"  {rec_path.name:35s}  EMPTY")
            continue

        transitions = extract_transitions(events, args.min_dist, args.speed_thresh, args.margin)

        for t in transitions:
            t["source"] = rec_path.name
            all_templates.append(t)

        dists = [t["orig_dist"] for t in transitions]
        durs  = [t["orig_dur"]  for t in transitions]

        if transitions:
            print(f"  {rec_path.name:35s}  {len(transitions):3d} templates   "
                  f"dist: {min(dists):.0f}–{max(dists):.0f}px   "
                  f"dur: {min(durs)*1000:.0f}–{max(durs)*1000:.0f}ms")
        else:
            print(f"  {rec_path.name:35s}    0 templates")

    # Save
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(all_templates, indent=1))

    print(f"\n{'='*60}")
    print(f"Total templates: {len(all_templates)}")
    print(f"Saved to: {out_file}")

    # Summary by distance bucket
    buckets = {"<50px": [], "50-150px": [], "150-300px": [], ">300px": []}
    for t in all_templates:
        d = t["orig_dist"]
        if   d < 50:  buckets["<50px"].append(t)
        elif d < 150: buckets["50-150px"].append(t)
        elif d < 300: buckets["150-300px"].append(t)
        else:         buckets[">300px"].append(t)

    print("\nBy distance:")
    for label, items in buckets.items():
        if items:
            durs = [t["orig_dur"] for t in items]
            print(f"  {label:12s}  {len(items):3d} templates   "
                  f"dur: {min(durs)*1000:.0f}–{max(durs)*1000:.0f}ms")
        else:
            print(f"  {label:12s}    0 templates")


if __name__ == "__main__":
    main()
