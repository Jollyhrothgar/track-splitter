---
name: track-splitter
description: Split a long recording of a rehearsal, jam, gig or practice session into individual songs, let the user trim and name each one in a small browser app, and export them at matched loudness (FLAC by default). Also lines up several mics or recorders that captured the same session, so each song gets one file per mic. Use this whenever the user has a long audio recording (WAV, FLAC, MP3, M4A, AIFF or OGG files, or an Audacity .aup4 project) and wants it cut into tracks, songs or takes; mentions splitting or chopping up a rehearsal or session recording; wants takes trimmed, named or volume-normalised; or wants recordings from different mics aligned — even if they never say "split".
---

# Track splitter

Turns a folder of long session recordings into named, trimmed, loudness-matched song files. The scripts do the tedious part (finding where the songs are, lining up mics, levelling); the user does the part that needs ears (where exactly each song starts and ends, and what it is called) in a browser page.

The scripts are in `scripts/` beside this file and run with `uv run`, which fetches their Python dependencies on first use. They were built and used on macOS. Originals are only ever read, never changed.

## The flow

Work in the **session folder**: the folder holding one session's recordings. These can be WAV, FLAC, MP3, M4A, AIFF or OGG files, or Audacity `.aup4` projects, in any mix.

### 1. Look at what is there

List the folder and work out which device made each recording. Ask the user if it isn't obvious. This matters because of mic names, below.

### 2. Cut the recordings into takes

```
uv run scripts/pair_mics.py --mic "Zoom" <session folder>                                   # one mic
uv run scripts/pair_mics.py --label aup4="Ear Trumpet" --label 261008=Zoom <session folder>  # several
```

This writes `<session folder>/takes/`, with every take numbered in the order it was played and one file per mic that caught it (`03 Take 02 - 4m39s - Zoom.wav`), plus `takes.json` recording what each file was cut from. Takes under 90 seconds are named "Fragment"; they are often false starts, but can be the tail of a song (see Adjusting).

**Mic names.** Every recording needs to be attributed to the right device, because recordings from *different* mics are compared to see if they overlap, and recordings from the *same* mic never are.

- One device for the whole session: pass `--mic NAME` and nothing is compared.
- Several devices: pass one `--label TEXT=NAME` per device, where TEXT is something its file names share (a date prefix, an extension). Files recorded with the Shure MOTIV app name themselves. An unlabelled file is treated as its own mic, named after the file.

With several mics, run once with `--dry-run` first and read the "Alignment" lines before writing anything. Each pair of recordings shows either an offset and a match score, or "no overlap". Check that these agree with what the user says happened. Scores for real overlaps have been 0.7 to 0.95; short recordings (under ten minutes) can produce false matches near the 0.6 threshold. A match that would put one mic in two places at once is discarded automatically and reported as "ignored".

### 3. Let the user trim and name

```
uv run scripts/trim_takes.py <session folder>/takes
```

Run this in the background: it is a small local web server (port 8377) and opens the page in the browser. It must keep running while the user works. Tell them how it works:

- Takes are listed on the left. Drag the two handles to the song's start and end, type its name, press Enter. That saves it and moves to the next take.
- Space plays and pauses, Q plays from the crop start, E plays the last three seconds, `,` and `.` nudge the last-moved handle. M (or 1, 2, 3) switches between mics at the same moment in the song.
- One trim applies to every mic's file of that take. Saving a take again replaces its earlier export.

Songs are written to `<session folder>/songs/`, as `Song Name.flac` or, with several mics, `Song Name - Mic.flac`. The menu beside the Save button sets the format (FLAC, MP3, WAV or M4A) for every song saved from then on, and the choice is remembered for the session. Every file is brought to the same loudness (-14 LUFS, about what streaming services use) with a limiter so nothing clips. Crops and names are remembered in `songs/.trim.json`.

Avoid restarting the server while the user is mid-session. If you must (to change an option), tell them to reload the page afterwards: the open tab loses its audio until they do.

### 4. Finish

When the user is done, stop the server. `songs/` is self-contained. `takes/` is only needed for more trimming, and the original recordings only for cutting takes again, so the user can delete either once they are happy.

## Adjusting

You cannot hear the audio, so report what the scripts measured and let the user judge. When they report a problem, these are the usual fixes:

| What the user sees | What to do |
|---|---|
| A song with pauses or stops in it came out as two takes (typically a take followed straight away by a short Fragment) | Re-run `pair_mics.py` with a larger `--merge-gap` (default 15 seconds). At 25, separate songs started being joined. |
| Two separate songs came out as one take | Smaller `--merge-gap` (8), or just trim it and save twice under two names. |
| A quiet song is missing, or talking was cut as a take | `--cutoff` on `split_rehearsal.py` sets how far below the loud passages still counts as playing; run it on the one recording to experiment (it prints what it finds). |
| Two recordings were lined up that should not have been, or the reverse | `--min-match` (default 0.6), or `--skip TEXT` to leave a recording out. |
| Songs sound squashed or pumping | Start the trimmer with a lower `--loudness` (for example -16), or `--no-master`. |
| Wants MP3 or another format | They pick it in the format menu on the page (or start the trimmer with `--format`). Songs already saved keep their format until saved again; `--resave` converts them all. |

**Re-cutting after the user has saved songs** is safe. `pair_mics.py` replaces the takes and carries the saved crops over to the new ones by their position in the original recordings (the previous list is kept as `songs/.trim.json.bak`). The exported song files are not touched, so if the set of mics, the format or the loudness changed, bring them up to date with:

```
uv run scripts/trim_takes.py --resave <session folder>/takes
```

## Things worth knowing

- **How songs are found.** A band playing holds its level up continuously; talking, tuning and noodling come in bursts. Detection looks for sustained level, measured relative to each recording's own loud passages, so input gain does not matter and stops of a few seconds inside a song are ignored.
- **Order of takes.** Recordings that overlap are ordered by the audio itself. Groups that do not overlap (say, before and after a break) are ordered by the files' modified times, which are wrong for files that have been copied from a recorder. If the order looks off, that is why.
- **Audacity projects** are read directly, including changes not yet saved in Audacity (its autosave). There is no need to export a WAV first.
- **Just want labels in Audacity?** `uv run scripts/split_rehearsal.py --open <recording>` writes an Audacity label file for one recording and opens both in Audacity, without cutting anything.
- **M4A needs a Mac.** Reading M4A recordings and saving M4A songs both use `afconvert`, which is part of macOS. The other formats have no such limit.
- **Disk space.** Takes are uncompressed copies of the musical parts of every recording, so expect `takes/` to be a large fraction of the originals' size.
- Each script's `--help` lists every option.
