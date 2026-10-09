# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "soundfile"]
# ///
"""Cut a session's recordings into takes, lining up different mics if there are several.

    uv run pair_mics.py --mic Zoom 2026-10-08
    uv run pair_mics.py --label aup4="Ear Trumpet" --label 261008=Zoom 2026-10-08

Works for one mic or many, with recordings as WAV, FLAC, MP3, M4A, AIFF or OGG
files or Audacity (.aup4) projects. Recordings that overlap in time are found by
matching their loudness over time, so nothing needs to be in sync beforehand.
Every take is cut at the same moment from each mic that caught it and written
to "<folder>/takes", numbered in the order they were played:

    03 Take 02 - 4m39s - Ear Trumpet.wav
    03 Take 02 - 4m39s - Shure MV88+.wav

A "takes.json" beside them records what each file was cut from. Running this
again (new mic, different settings) replaces the takes; crops already saved
with trim_takes.py are carried over to the new takes automatically.
Song detection is split_rehearsal.py's.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

import split_rehearsal as split

ENVELOPE_RATE = 100  # per second, for fine alignment
SEARCH = 3.0  # seconds either side of the rough alignment to search


def mic_name(path, labels):
    for pattern, name in labels:
        if pattern in path.name:
            return name
    try:  # recordings made with Shure's MOTIV app carry the mic's name (macOS only)
        tagged = subprocess.run(["xattr", "-p", "com.shure.motiv.microphoneUsed", str(path)], capture_output=True, text=True)
        if tagged.returncode == 0 and tagged.stdout.strip():
            return tagged.stdout.strip()
    except FileNotFoundError:
        pass
    return path.stem


def shape(db):
    """A recording's loudness over time, reduced to what two mics would agree on."""
    rel = np.where(np.isfinite(db), db - np.nanpercentile(db, 95), -40.0)
    return np.clip(rel, -40.0, 0.0)


def rough_offset(x, y, min_overlap=120):
    """Seconds by which y starts after x, and how well the two then agree (-1 to 1)."""
    best = (-1.0, 0)
    for d in range(min_overlap - len(y), len(x) - min_overlap + 1):
        a = x[max(d, 0) : min(len(x), d + len(y))]
        b = y[max(-d, 0) : max(-d, 0) + len(a)]
        a, b = a - a.mean(), b - b.mean()
        denom = np.sqrt((a * a).sum() * (b * b).sum())
        if denom > 0:
            best = max(best, (float((a * b).sum() / denom), d))
    return best[1], best[0]


def read(rec, start, end):
    """Audio between two times in a recording's own clock, clamped to what exists."""
    start, end = max(start, rec["span"][0]), min(end, rec["span"][1])
    if rec["path"].suffix.lower() == ".aup4":
        return split.project_audio(rec["path"], start, end)
    return split.wav_audio(rec["path"], start, end)


def read_padded(rec, start, end):
    """Like read, but silent where the recording doesn't reach, so every mic's file of a take lines up."""
    audio, rate = read(rec, start, end)
    before = round(max(rec["span"][0] - start, 0.0) * rate)
    after = max(round((end - start) * rate) - before - len(audio), 0)
    return np.pad(audio, ((before, after), (0, 0))), rate


def onsets(audio, rate):
    """How sharply the level rises, ENVELOPE_RATE times a second."""
    hop = round(rate / ENVELOPE_RATE)
    mono = audio.mean(axis=1)
    frames = mono[: len(mono) // hop * hop].reshape(-1, hop)
    level = np.log10((frames * frames).mean(axis=1) + 1e-10)
    rise = np.maximum(np.diff(level), 0.0)
    return rise - rise.mean()


def fine_shift(ref_audio, ref_rate, ref_skip, other, start, end):
    """Correction in seconds to add to `other`'s rough position, from the part of the take both mics have.

    `ref_audio` begins `ref_skip` seconds into the take; `start` and `end` are the take on `other`'s clock.
    """
    have_from = max(start, other["span"][0]) - start  # seconds into the take
    have_to = min(end, other["span"][1]) - start
    # The same stretch of the reference, less a margin to slide within.
    lo = max(round((have_from + SEARCH - ref_skip) * ref_rate), 0)
    hi = min(round((have_to - SEARCH - ref_skip) * ref_rate), len(ref_audio))
    if hi - lo < 2 * SEARCH * ref_rate:
        return 0.0
    want = onsets(ref_audio[lo:hi], ref_rate)
    audio, rate = read(other, start + have_from, start + have_to)
    have = onsets(audio, rate)
    if len(have) <= len(want):
        return 0.0
    return int(np.argmax(np.correlate(have, want, mode="valid"))) / ENVELOPE_RATE - (lo / ref_rate + ref_skip - have_from)


def merge(regions, more):
    """Combine two lists of [start, end] on the same clock, widening regions both lists found."""
    out, used = [], set()
    for a, b in regions:
        best, overlap = None, 0.0
        for i, (c, d) in enumerate(more):
            shared = min(b, d) - max(a, c)
            if i not in used and shared > overlap and shared >= 0.5 * min(b - a, d - c):
                best, overlap = i, shared
        if best is None:
            out.append([a, b])
        else:
            used.add(best)
            out.append([min(a, more[best][0]), max(b, more[best][1])])
    for i, (c, d) in enumerate(more):
        if i in used:
            continue
        # A leftover that sits mostly inside a region is part of it: one mic split what another heard whole.
        inside = [r for r in out if min(r[1], d) - max(r[0], c) >= 0.5 * (d - c)]
        if inside:
            inside[0][0], inside[0][1] = min(inside[0][0], c), max(inside[0][1], d)
        else:
            out.append([c, d])
    out.sort()
    joined = []
    for r in out:  # widening can swallow a neighbour; if it covers most of it, they are one take
        if joined and joined[-1][1] - r[0] >= 0.5 * min(r[1] - r[0], joined[-1][1] - joined[-1][0]):
            joined[-1][1] = max(joined[-1][1], r[1])
        else:
            joined.append(r)
    for prev, cur in zip(joined, joined[1:]):
        if cur[0] < prev[1]:
            prev[1] = cur[0] = (prev[1] + cur[0]) / 2
    return [r for r in joined if r[1] > r[0]]


def carry_crops(old, new, crops):
    """Re-key crops saved by trim_takes.py to a new set of takes, by where they sit in the original recordings.

    Returns the new crops and the names of takes whose crop no longer fits inside any take.
    """
    was_cut_from = {take["take"]: take["mics"] for take in old}
    carried, lost = {}, []
    for name, crop in crops.items():
        for was in was_cut_from.get(name, []):
            a, b = was["from"] + crop["start"], was["from"] + crop["end"]
            now = next(((take["take"], mic) for take in new for mic in take["mics"]
                        if mic["recording"] == was["recording"] and mic["from"] - 1 <= a and b <= mic["from"] + mic["length"] + 1), None)
            if now:
                carried[now[0]] = dict(crop, start=max(a - now[1]["from"], 0.0), end=min(b - now[1]["from"], now[1]["length"]))
                break
        else:
            lost.append(name)
            carried[name] = crop  # kept, so nothing is silently dropped
    return carried, lost


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="session folder of recordings (audio files and Audacity .aup4 projects)")
    parser.add_argument("--label", action="append", default=[], metavar="TEXT=NAME", help='mic name for recordings whose file name contains TEXT, e.g. aup4="Ear Trumpet"')
    parser.add_argument("--mic", metavar="NAME", help="every recording in the folder came from this one mic (skips alignment)")
    parser.add_argument("--skip", action="append", default=[], metavar="TEXT", help="leave out recordings whose file name contains TEXT")
    parser.add_argument("--manifest", type=Path, help='where to record what each take was cut from (default: "takes.json" in the takes folder)')
    parser.add_argument("--merge-gap", type=int, default=15, help="join stretches of playing closer than this, seconds; lower it if separate songs get joined (default 15)")
    parser.add_argument("--min-match", type=float, default=0.6, help="how well two recordings must agree to count as the same event, 0-1 (default 0.6)")
    parser.add_argument("--no-normalize", action="store_true", help="keep the original level instead of raising each file's peak to -1 dB")
    parser.add_argument("--dry-run", action="store_true", help="show the alignment and the takes without writing files")
    parser.add_argument("--open", action="store_true", help="open the takes folder in Finder")
    args = parser.parse_args()
    labels = [tuple(item.split("=", 1)) for item in args.label]

    paths = sorted(p for p in args.folder.iterdir() if p.suffix.lower() in (*split.AUDIO, ".aup4") and not p.name.startswith(".") and not any(s in p.name for s in args.skip))
    if not paths:
        sys.exit("No recordings found (looked for " + ", ".join(split.AUDIO) + " and .aup4).")
    recs = []
    for path in paths:
        is_project = path.suffix.lower() == ".aup4"
        db = split.project_levels(path) if is_project else split.levels(path)
        songs, _ = split.find_songs(db, window=15, merge_gap=args.merge_gap, min_length=30, pad=1.5)
        heard = np.flatnonzero(np.isfinite(db))
        # Wall-clock start, only used to put unrelated recordings in order.
        began = path.stat().st_mtime - len(db) if not is_project else None
        recs.append({"path": path, "mic": args.mic or mic_name(path, labels), "db": db, "songs": songs,
                     "span": (float(heard[0]), float(heard[-1] + 1)), "began": began, "group": None, "offset": 0.0})

    # Find which recordings overlap, and place each group on one shared clock.
    print("Alignment")
    found = []
    for i, x in enumerate(recs):
        for j in range(i + 1, len(recs)):
            y = recs[j]
            if x["mic"] == y["mic"]:
                continue  # one mic can't have recorded the same moment twice
            d, match = rough_offset(shape(x["db"]), shape(y["db"]))
            if match >= args.min_match:
                found.append((match, i, j, d))
            else:
                print(f"  {x['path'].name}  +  {y['path'].name}: no overlap found (best match {match:.2f})")
    # Join recordings strongest match first. A match is dropped if it would put
    # one mic in two places at once, which is how a false match shows itself.
    groups = [[rec] for rec in recs]
    for rec, group in zip(recs, groups):
        rec["group"] = group
    for match, i, j, d in sorted(found, reverse=True):
        x, y = recs[i], recs[j]
        pair = f"  {x['path'].name}  +  {y['path'].name}: "
        if x["group"] is y["group"]:
            agrees = abs(y["offset"] - (x["offset"] + d)) <= 5
            print(pair + (f"consistent with the others (match {match:.2f})" if agrees else f"ignored, contradicts stronger matches (match {match:.2f})"))
            continue
        move = x["offset"] + d - y["offset"]
        clash = any(
            p["mic"] == q["mic"] and min(p["span"][1] + p["offset"], q["span"][1] + q["offset"] + move) - max(p["span"][0] + p["offset"], q["span"][0] + q["offset"] + move) > 5
            for p in x["group"] for q in y["group"]
        )
        if clash:
            print(pair + f"ignored, would overlap two recordings from one mic (match {match:.2f})")
            continue
        print(pair + f"second starts {split.clock(abs(d))} {'after' if d >= 0 else 'before'} first (match {match:.2f})")
        merged = x["group"]
        for rec in y["group"]:
            rec["offset"] += move
            rec["group"] = merged
            merged.append(rec)
    groups = [g for n, g in enumerate(groups) if all(g is not h for h in groups[:n]) and g and g[0]["group"] is g]

    def group_start(group):
        known = [r["began"] - r["offset"] for r in group if r["began"] is not None]
        return min(known) if known else float("inf")

    groups.sort(key=group_start)

    out = args.folder / "takes"
    previous = None
    if not args.dry_run:
        out.mkdir(exist_ok=True)
        if (out / "takes.json").exists():
            previous = json.loads((out / "takes.json").read_text())
        for old in out.glob("*.wav"):  # takes from an earlier run, which may be numbered differently
            if re.match(r"\d+ (Take|Fragment) \d+ - \d+m\d\ds - ", old.name):
                old.unlink()
    manifest = []
    print("\nTakes")
    number = takes = fragments = 0
    for group in groups:
        regions = []
        for rec in group:
            regions = merge(regions, [[a + rec["offset"], b + rec["offset"]] for a, b in rec["songs"]])
        for a, b in regions:
            # Mics that caught at least half of this take; the one that caught most is the reference.
            def caught(r):
                return min(b, r["span"][1] + r["offset"]) - max(a, r["span"][0] + r["offset"])

            mics = sorted((r for r in group if caught(r) >= 0.5 * (b - a)), key=caught, reverse=True)
            if not mics:
                continue
            number += 1
            if b - a < 90:
                fragments += 1
                name = f"Fragment {fragments:02d}"
            else:
                takes += 1
                name = f"Take {takes:02d}"
            stem = f"{number:02d} {name} - {int(b - a) // 60}m{int(b - a) % 60:02d}s"
            ref = mics[0]
            ref_audio, ref_rate = None, None
            notes = []
            entry = {"take": stem, "mics": []}
            manifest.append(entry)
            for rec in mics:
                start, end = a - rec["offset"], b - rec["offset"]
                shift = 0.0
                if rec is ref:
                    if len(mics) > 1:
                        ref_audio, ref_rate = read(rec, start, end)
                    if not args.dry_run:
                        audio, rate = read_padded(rec, start, end)
                    notes.append(rec["mic"])
                else:
                    ref_skip = max(ref["span"][0] - (a - ref["offset"]), 0.0)
                    shift = fine_shift(ref_audio, ref_rate, ref_skip, rec, start, end)
                    missing = (b - a) - caught(rec)
                    notes.append(f"{rec['mic']} ({shift:+.2f}s" + (f", missing {missing:.0f}s)" if missing >= 1 else ")"))
                    if not args.dry_run:
                        audio, rate = read_padded(rec, start + shift, end + shift)
                # Where this mic's file begins, on its own recording's clock.
                entry["mics"].append({"mic": rec["mic"], "file": f"{stem} - {rec['mic']}.wav", "recording": rec["path"].name, "from": start + shift, "length": b - a})
                if args.dry_run:
                    continue
                peak = float(np.abs(audio).max())
                if peak > 0 and not args.no_normalize:
                    audio = audio * (10 ** (-1 / 20) / peak)
                sf.write(out / f"{stem} - {rec['mic']}.wav", audio, rate, subtype="PCM_24")
            print(f"  {stem}:  {'  +  '.join(notes)}")
    if args.manifest or not args.dry_run:
        (args.manifest or out / "takes.json").write_text(json.dumps(manifest, indent=1))
    crops = args.folder / "songs" / ".trim.json"
    if not args.dry_run and previous and crops.exists():
        saved = json.loads(crops.read_text())
        carried, lost = carry_crops(previous, manifest, saved)
        crops.with_name(".trim.json.bak").write_text(json.dumps(saved, indent=1))
        crops.write_text(json.dumps(carried, indent=1))
        print(f"\nCarried {len(saved) - len(lost)} saved crops over to the new takes.")
        for name in lost:
            print(f"  could not place the crop for {name}; trim that song again")
    if not args.dry_run:
        print(f"\n-> {out}/")
        if args.open:
            subprocess.run(["open", str(out)], check=True)


if __name__ == "__main__":
    main()
