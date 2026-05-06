"""
process_data.py
───────────────
Reads SkillCorner tracking data (JSONL) + match metadata (JSON).
Outputs two files consumed by the Google Site HTML pages:
  • player_speeds.json   – per-player speed stats (avg, max, per-minute)
  • pitch_frames.json    – ~30 sampled frames with positions & velocities
                          for the pitch-control visualisation

Run once before uploading everything to GitHub Pages / hosting.
"""

import json
import math
import os

BASE = os.path.dirname(os.path.abspath(__file__))
TRACKING_FILE = os.path.join(BASE, "2064380_tracking_extrapolated.jsonl")
MATCH_FILE    = os.path.join(BASE, "2064380_match_data.json")
OUT_SPEEDS    = os.path.join(BASE, "player_speeds.json")
OUT_FRAMES    = os.path.join(BASE, "pitch_frames.json")

FPS           = 25          # SkillCorner standard frame rate
SMOOTH_WIN    = 5           # frames used for velocity smoothing (rolling avg)
PITCH_W       = 105.0       # metres (from match data)
PITCH_H       = 68.0
MAX_SPEED     = 10.0        # m/s (~36 km/h) – realistic football max sprint
MAX_DIST_PER_FRAME = MAX_SPEED / FPS  # max distance a player can move in 1 frame (0.40 m)

# ── 1.  Load match metadata ───────────────────────────────────────────────────
print("Loading match data …")
with open(MATCH_FILE) as f:
    match = json.load(f)

home_id = match["home_team"]["id"]
away_id = match["away_team"]["id"]
home_name = match["home_team"]["name"]
away_name = match["away_team"]["name"]

# Build player look-up: trackable_object → {name, team_id, position, number}
player_meta = {}
for p in match["players"]:
    to_id = p.get("trackable_object")
    if to_id is None:
        to_id = p["id"]
    player_meta[p["id"]] = {
        "id":       p["id"],
        "name":     p.get("short_name", f"{p['first_name']} {p['last_name']}"),
        "team_id":  p["team_id"],
        "team":     home_name if p["team_id"] == home_id else away_name,
        "team_side":"home"    if p["team_id"] == home_id else "away",
        "position": p.get("player_role", {}).get("name", ""),
        "pos_group":p.get("player_role", {}).get("position_group", ""),
        "number":   p.get("number", ""),
        "trackable_object": to_id,
    }

# ── 2.  Stream tracking file – build position history per player ──────────────
print("Streaming tracking data …")

# Structures
pos_history   = {}   # player_id → deque of (frame, x, y)
speed_history = {}   # player_id → list of speeds (m/s)
prev_pos      = {}   # player_id → (frame, x, y)

# We also collect frames that are "in-play" (period 1 or 2, ball detected).
# We'll keep up to 600 evenly-spaced in-play frames for the timeline.
in_play_frames = []  # list of {frame, timestamp, period, ball, players:[{id,x,y,vx,vy,speed}]}

frame_buffer = {}    # frame → raw data   (keep last SMOOTH_WIN frames)
ring = []            # circular buffer of last SMOOTH_WIN frames

MAX_KEEP_FRAMES = 600   # maximum frames exported for the site

with open(TRACKING_FILE) as f:
    for raw_line in f:
        frame_data = json.loads(raw_line)
        frame_no   = frame_data["frame"]
        period     = frame_data.get("period")
        if period is None:
            continue                         # pre-match / half-time

        players = frame_data.get("player_data", [])
        if not players:
            continue

        ball = frame_data.get("ball_data", {})
        ball_x = ball.get("x")
        ball_y = ball.get("y")

        # ── per-player velocity ──────────────────────────────────────────────
        frame_players = []
        for p in players:
            pid = p["player_id"]
            x, y = p.get("x"), p.get("y")
            detected = p.get("is_detected", False)
            if x is None or y is None:
                continue

            vx, vy, spd = 0.0, 0.0, 0.0
            if pid in prev_pos:
                pf, px, py, _ = prev_pos[pid]
                dt = (frame_no - pf) / FPS
                if dt > 0 and dt <= 0.2:   # only use consecutive/near frames (≤5 frames apart)
                    vx  = (x - px) / dt
                    vy  = (y - py) / dt
                    spd = math.hypot(vx, vy)
                    if spd > MAX_SPEED:
                        scale = MAX_SPEED / spd
                        vx *= scale; vy *= scale
                        spd = MAX_SPEED
                    speed_history.setdefault(pid, []).append(spd)

            prev_pos[pid] = (frame_no, x, y, detected)
            frame_players.append({
                "id":    pid,
                "x":     round(x,  2),
                "y":     round(y,  2),
                "vx":    round(vx, 3),
                "vy":    round(vy, 3),
                "speed": round(spd, 3),
            })

        in_play_frames.append({
            "frame":     frame_no,
            "timestamp": frame_data.get("timestamp", ""),
            "period":    period,
            "ball":      {"x": ball_x, "y": ball_y,
                          "detected": ball.get("is_detected", False)},
            "possession": frame_data.get("possession", {}),
            "players":   frame_players,
        })

print(f"  In-play frames collected: {len(in_play_frames)}")

# ── 3.  Thin the frame list to MAX_KEEP_FRAMES ────────────────────────────────
if len(in_play_frames) > MAX_KEEP_FRAMES:
    step = len(in_play_frames) / MAX_KEEP_FRAMES
    in_play_frames = [in_play_frames[int(i * step)] for i in range(MAX_KEEP_FRAMES)]

print(f"  Frames after thinning:    {len(in_play_frames)}")

# ── 4.  Build player speed table ─────────────────────────────────────────────
print("Computing speed statistics …")
speed_table = []
for pid, meta in player_meta.items():
    speeds = speed_history.get(pid, [])
    if not speeds:
        avg_spd = max_spd = 0.0
    else:
        avg_spd = sum(speeds) / len(speeds)
        # "Max speed" = median of top-50 readings → robust to short artefact spikes
        sorted_s = sorted(speeds, reverse=True)
        top50    = sorted_s[:min(50, len(sorted_s))]
        max_spd  = sorted(top50)[len(top50) // 2]   # median of top-50

    # speed zones (m/s thresholds)
    def zone_pct(lo, hi):
        if not speeds: return 0
        return round(100 * sum(1 for s in speeds if lo <= s < hi) / len(speeds), 1)

    speed_table.append({
        "id":          pid,
        "name":        meta["name"],
        "team":        meta["team"],
        "team_side":   meta["team_side"],
        "position":    meta["position"],
        "pos_group":   meta["pos_group"],
        "number":      meta["number"],
        "avg_speed":   round(avg_spd, 2),   # m/s
        "max_speed":   round(max_spd, 2),   # m/s
        "avg_speed_kmh": round(avg_spd * 3.6, 1),
        "max_speed_kmh": round(max_spd * 3.6, 1),
        "walk_pct":    zone_pct(0,   2),
        "jog_pct":     zone_pct(2,   4),
        "run_pct":     zone_pct(4,   5.5),
        "hsr_pct":     zone_pct(5.5, 7),
        "sprint_pct":  zone_pct(7,   12),
    })

speed_table.sort(key=lambda r: r["max_speed_kmh"], reverse=True)

# ── 5.  Write outputs ─────────────────────────────────────────────────────────
output_speeds = {
    "match": {
        "home_team": home_name,
        "away_team": away_name,
        "home_score": match["home_team_score"],
        "away_score": match["away_team_score"],
        "date":  match["date_time"][:10],
        "venue": match["stadium"]["name"],
        "pitch_length": PITCH_W,
        "pitch_width":  PITCH_H,
    },
    "players": speed_table,
}

output_frames = {
    "meta": {
        "home_team":    home_name,
        "away_team":    away_name,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "pitch_length": PITCH_W,
        "pitch_width":  PITCH_H,
        "fps":          FPS,
        "player_meta":  {str(k): v for k, v in player_meta.items()},
    },
    "frames": in_play_frames,
}

print(f"Writing {OUT_SPEEDS} …")
with open(OUT_SPEEDS, "w") as f:
    json.dump(output_speeds, f, separators=(",", ":"))

print(f"Writing {OUT_FRAMES} …")
with open(OUT_FRAMES, "w") as f:
    json.dump(output_frames, f, separators=(",", ":"))

sizes = {
    "player_speeds.json": os.path.getsize(OUT_SPEEDS) / 1024,
    "pitch_frames.json":  os.path.getsize(OUT_FRAMES) / 1024,
}
print("\n✅  Done!")
for name, kb in sizes.items():
    print(f"   {name}: {kb:.1f} KB")
