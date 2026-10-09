# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "soundfile"]
# ///
"""Find the songs in rehearsal recordings and write Audacity label files.

    uv run split_rehearsal.py 2026-10-08            # every recording in the folder
    uv run split_rehearsal.py path/to/recording.wav
    uv run split_rehearsal.py --open path/to/project.aup4

Recordings can be WAV, FLAC, MP3, M4A, AIFF or OGG files, or Audacity (.aup4)
projects. For each recording this writes "<recording>.labels.txt" next to it. In Audacity,
open the recording, import that file as labels, fix up the regions and names,
then export the labelled regions as separate files. --open hands both files to
Audacity for you. Audacity projects are read directly and never modified.

    uv run split_rehearsal.py --export --open 2026-10-08

--export skips Audacity: each region is written to its own file in a
"<recording> takes" folder, ready to listen to and rename.

Detection looks for stretches where the level stays up continuously (a band
playing) rather than coming in bursts (talking, tuning, noodling). Levels are
first normalised to each recording's loud passages, so the cut-off is comparable
between recordings whatever the input gain was. The audio itself is not changed.
"""
import argparse
import atexit
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf


AUDIO = (".wav", ".flac", ".mp3", ".m4a", ".aif", ".aiff", ".ogg")
converted = {}


def readable(path):
    """A path soundfile can open: the file itself, or a temporary WAV copy of an M4A, which it cannot read."""
    path = Path(path)
    if path.suffix.lower() != ".m4a":
        return path
    if path not in converted:
        if not shutil.which("afconvert"):
            sys.exit(f"{path.name}: reading M4A files uses afconvert, which is part of macOS. Convert the file to WAV or FLAC first.")
        handle, copy = tempfile.mkstemp(suffix=".wav")
        os.close(handle)
        atexit.register(os.unlink, copy)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI24", str(path), copy], check=True)
        converted[path] = Path(copy)
    return converted[path]


def levels(path):
    """Level of each second of the recording, in dB."""
    out = []
    with sf.SoundFile(readable(path)) as f:
        for block in f.blocks(blocksize=f.samplerate, dtype="float32", always_2d=True):
            mono = block.mean(axis=1)
            out.append(10 * np.log10(np.mean(mono * mono) + 1e-12))
    return np.array(out)


def read_project_xml(blobs):
    """Decode Audacity's binary project XML into (kind, name, value) events."""
    names, size = {}, 1
    for b in blobs:
        p = 0
        while p < len(b):
            kind = b[p]
            p += 1
            if kind == 0:  # character size
                size = b[p]
                p += 1
            elif kind in (13, 14):  # push / pop
                pass
            elif kind in (11, 12):  # text outside tags
                p += 4 + struct.unpack_from("<i", b, p)[0]
            else:
                (ident,) = struct.unpack_from("<H", b, p)
                p += 2
                if kind == 15:  # name definition
                    (n,) = struct.unpack_from("<H", b, p)
                    names[ident] = b[p + 2 : p + 2 + n].decode(f"utf-{8 * size}-le" if size > 1 else "utf-8")
                    p += 2 + n
                elif kind in (1, 2):
                    yield ("start" if kind == 1 else "end", names[ident], None)
                elif kind in (3, 16):  # string / binary
                    p += 4 + struct.unpack_from("<i", b, p)[0]
                elif kind in (4, 6, 8):
                    yield "attr", names[ident], struct.unpack_from("<i", b, p)[0]
                    p += 4
                elif kind == 5:
                    p += 1
                elif kind == 7:
                    yield "attr", names[ident], struct.unpack_from("<q", b, p)[0]
                    p += 8
                elif kind == 9:
                    yield "attr", names[ident], struct.unpack_from("<f", b, p)[0]
                    p += 8
                elif kind == 10:
                    yield "attr", names[ident], struct.unpack_from("<d", b, p)[0]
                    p += 12
                else:
                    raise ValueError(f"{kind}: unrecognised field in Audacity project")


SAMPLE_FORMATS = {0x0004000F: ("<f4", 1.0), 0x00020001: ("<i2", 32768.0), 0x00040002: ("<i4", 8388608.0)}


def open_project(path):
    """Open an Audacity project read-only and list its clips."""
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    # The autosave holds changes not yet saved, which is what Audacity shows.
    row = db.execute("SELECT dict, doc FROM autosave").fetchone() or db.execute("SELECT dict, doc FROM project").fetchone()
    clips, stack, rate, track = [], [], 44100.0, -1
    for kind, name, value in read_project_xml(row):
        if kind == "start":
            stack.append(name)
            if name == "wavetrack":
                track += 1
            elif name == "waveclip":
                clips.append({"rate": rate, "track": track, "offset": 0.0, "trimLeft": 0.0, "trimRight": 0.0, "blocks": []})
            elif name == "waveblock":
                clips[-1]["blocks"].append({})
        elif kind == "end":
            stack.pop()
        elif stack[-1] == "wavetrack" and name == "rate":
            rate = value
        elif stack[-1] == "waveclip" and name in ("offset", "trimLeft", "trimRight"):
            clips[-1][name] = value
        elif stack[-1] == "sequence" and name == "numsamples":
            clips[-1]["samples"] = value
        elif stack[-1] == "waveblock" and name in ("start", "length", "blockid"):
            clips[-1]["blocks"][-1][name] = value
    return db, clips


def clip_audio(db, clip, first=0, last=None):
    """Yield (timeline sample position, samples) for the visible part of a clip."""
    rate = clip["rate"]
    shift = round(clip["offset"] * rate)
    lo = max(round(clip["trimLeft"] * rate), first - shift)
    hi = clip["samples"] - round(clip["trimRight"] * rate)
    if last is not None:
        hi = min(hi, last - shift)
    for block in clip["blocks"]:
        start = block["start"]
        if block.get("blockid", 0) <= 0 or start >= hi or start + block.get("length", 1 << 30) <= lo:
            continue  # silence, or outside the wanted range
        fmt, raw = db.execute("SELECT sampleformat, samples FROM sampleblocks WHERE blockid = ?", (block["blockid"],)).fetchone()
        dtype, scale = SAMPLE_FORMATS[fmt]
        x = np.frombuffer(raw, dtype=dtype).astype(np.float32) / scale
        a, b = max(lo - start, 0), min(hi - start, len(x))
        if b > a and start + a + shift >= 0:
            yield start + a + shift, x[a:b]


def project_levels(path):
    """Level of each second of an Audacity project's timeline, in dB."""
    db, clips = open_project(path)
    try:
        energy, count = np.zeros(0), np.zeros(0)
        for clip in clips:
            for position, x in clip_audio(db, clip):
                sec = ((position + np.arange(len(x))) // clip["rate"]).astype(int)
                size = max(len(energy), sec[-1] + 1)
                energy = np.pad(energy, (0, size - len(energy)))
                count = np.pad(count, (0, size - len(count)))
                energy += np.bincount(sec, weights=x.astype(np.float64) ** 2, minlength=size)
                count += np.bincount(sec, minlength=size)
    finally:
        db.close()
    return np.where(count > 0, 10 * np.log10(energy / np.maximum(count, 1) + 1e-12), np.nan)


def project_audio(path, start, end):
    """Audio between two times on a project's timeline: one channel per track, or a mono mix beyond two."""
    db, clips = open_project(path)
    try:
        rate = int(clips[0]["rate"])
        first, last = round(start * rate), round(end * rate)
        tracks = max(c["track"] for c in clips) + 1
        out = np.zeros((last - first, tracks if tracks <= 2 else 1), dtype=np.float32)
        for clip in clips:
            channel = clip["track"] if tracks <= 2 else 0
            for position, x in clip_audio(db, clip, first, last):
                out[position - first : position - first + len(x), channel] += x
    finally:
        db.close()
    return out, rate


def wav_audio(path, start, end):
    with sf.SoundFile(readable(path)) as f:
        f.seek(round(start * f.samplerate))
        return f.read(round((end - start) * f.samplerate), dtype="float32", always_2d=True), f.samplerate


def otsu(values):
    """Threshold that best separates values into a low and a high group."""
    hist, edges = np.histogram(values, bins=120)
    centres = (edges[:-1] + edges[1:]) / 2
    best, threshold = 0.0, edges[len(edges) // 2]
    for i in range(1, len(hist)):
        w0, w1 = hist[:i].sum(), hist[i:].sum()
        if not w0 or not w1:
            continue
        m0 = (hist[:i] * centres[:i]).sum() / w0
        m1 = (hist[i:] * centres[i:]).sum() / w1
        score = w0 * w1 * (m0 - m1) ** 2
        if score > best:
            best, threshold = score, edges[i]
    return threshold


def find_songs(db, window, merge_gap, min_length, pad, cutoff_range=(-16.0, -9.0)):
    """Return [start, end] pairs in seconds, and the cut-off used."""
    # Measure everything relative to the recording's loud passages, so the
    # cut-off means the same thing whatever the input gain was.
    # Stretches with no audio at all (NaN) are left out of the statistics.
    audio = np.isfinite(db)
    db = np.where(audio, np.maximum(db - np.nanpercentile(db, 95), -60.0), -60.0)
    half = window // 2
    padded = np.pad(db, (half, half), mode="edge")
    sustained = np.percentile(np.lib.stride_tricks.sliding_window_view(padded, window), 20, axis=1)
    threshold = float(np.clip(otsu(sustained[audio]), *cutoff_range))
    playing = sustained > threshold

    runs, start = [], None
    for i, on in enumerate(playing):
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(playing)])

    songs = []
    for a, b in runs:
        if songs and a - songs[-1][1] < merge_gap:
            songs[-1][1] = b
        else:
            songs.append([a, b])
    songs = [s for s in songs if s[1] - s[0] >= min_length]

    # The sustained level lags the real start and end, so walk each edge
    # outwards while the recording is still loud, then add a little padding.
    for i, (a, b) in enumerate(songs):
        lo = songs[i - 1][1] if i else 0
        hi = songs[i + 1][0] if i + 1 < len(songs) else len(db)
        while a > lo and a > songs[i][0] - window and db[a - 1] > threshold:
            a -= 1
        while b < hi and b < songs[i][1] + window and db[b] > threshold:
            b += 1
        songs[i] = [a, b]
    for i, (a, b) in enumerate(songs):
        lo = (songs[i - 1][1] + a) / 2 if i else 0
        hi = (b + songs[i + 1][0]) / 2 if i + 1 < len(songs) else len(db)
        songs[i] = [max(lo, a - pad), min(hi, b + pad)]
    return songs, threshold


def clock(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", type=Path, help="recordings, Audacity projects, or folders of them")
    parser.add_argument("--open", action="store_true", help="open each recording and its labels in Audacity")
    parser.add_argument("--export", action="store_true", help='write each region to its own file in a "<recording> takes" folder')
    parser.add_argument("--no-normalize", action="store_true", help="with --export, keep the original level instead of raising each file's peak to -1 dB")
    parser.add_argument("--app", default="Audacity 4", help='application used by --open (default "Audacity 4")')
    parser.add_argument("--min-length", type=int, default=30, help="shortest region to keep, seconds (default 30)")
    parser.add_argument("--merge-gap", type=int, default=8, help="join regions closer than this, seconds (default 8)")
    parser.add_argument("--window", type=int, default=15, help="how long the level must hold up, seconds (default 15)")
    parser.add_argument("--pad", type=float, default=1.5, help="extra time kept at each end, seconds (default 1.5)")
    parser.add_argument("--cutoff", type=float, help="fixed cut-off in dB below the loud passages, e.g. -12 (default: automatic, between -16 and -9)")
    parser.add_argument("--fragment", type=int, default=90, help='regions shorter than this are named "Fragment" (default 90)')
    args = parser.parse_args()

    recordings = []
    for path in args.paths:
        if path.is_dir():
            recordings += sorted(p for p in path.iterdir() if p.suffix.lower() in (*AUDIO, ".aup4") and not p.name.startswith("."))
        else:
            recordings.append(path)
    if not recordings:
        sys.exit("No recordings found (looked for " + ", ".join(AUDIO) + " and .aup4).")

    for wav in recordings:
        db = project_levels(wav) if wav.suffix.lower() == ".aup4" else levels(wav)
        cutoff_range = (args.cutoff, args.cutoff) if args.cutoff is not None else (-16.0, -9.0)
        songs, threshold = find_songs(db, args.window, args.merge_gap, args.min_length, args.pad, cutoff_range)
        out = wav.with_suffix(".labels.txt")
        print(f"\n{wav.name}  ({clock(len(db))}, loud passages at {np.nanpercentile(db, 95):.0f} dB, cut-off {threshold:.1f} dB below)")
        is_project = wav.suffix.lower() == ".aup4"
        folder = wav.with_name(wav.stem + " takes")
        if args.export:
            folder.mkdir(exist_ok=True)
        takes = fragments = 0
        with open(out, "w") as f:
            for a, b in songs:
                if b - a < args.fragment:
                    fragments += 1
                    name = f"Fragment {fragments:02d}"
                else:
                    takes += 1
                    name = f"Take {takes:02d}"
                f.write(f"{a:.6f}\t{b:.6f}\t{name}\n")
                print(f"  {clock(a):>6} - {clock(b):>6}  {clock(b - a):>5}  {name}")
                if args.export:
                    audio, rate = project_audio(wav, a, b) if is_project else wav_audio(wav, a, b)
                    peak = float(np.abs(audio).max())
                    if peak > 0 and not args.no_normalize:
                        audio *= 10 ** (-1 / 20) / peak
                    length = f"{int(b - a) // 60}m{int(b - a) % 60:02d}s"  # in the name, as Finder's Duration column can lag
                    sf.write(folder / f"{takes + fragments:02d} {name} - {length}.wav", audio, rate, subtype="PCM_24")
        print(f"  -> {out.name}")
        if args.export:
            print(f"  -> {folder.name}/")
            if args.open:
                subprocess.run(["open", str(folder)], check=True)
        elif args.open:
            subprocess.run(["open", "-a", args.app, str(wav), str(out)], check=True)


if __name__ == "__main__":
    main()
