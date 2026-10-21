# track-splitter

A [Claude Code](https://claude.com/claude-code) plugin that turns long recordings of a rehearsal, jam or gig into individual, named songs.

You point Claude at a folder of recordings and ask it to split the session. It finds where the songs are, cuts them into takes, and opens a small page in your browser where you trim each take, name it and save it. Songs come out at matched loudness, as FLAC, MP3, WAV or M4A. If several mics recorded the same session, it lines them up and gives you one file per mic for each song.

## Install

You need [uv](https://docs.astral.sh/uv/), which runs the scripts and fetches what they depend on.

```
claude plugin marketplace add Jollyhrothgar/track-splitter
claude plugin install track-splitter@track-splitter
```

`claude plugin update track-splitter@track-splitter` picks up new versions. Add `-s project` to the install line to turn it on for one project only.

Without the plugin system, install the skill from a clone instead:

```
git clone https://github.com/Jollyhrothgar/track-splitter.git
cd track-splitter
./install.sh                  # for every project
./install.sh --local [DIR]    # or for one project
```

That links the skill to the clone, so `git pull` updates it. Add `--copy` to copy the files instead. `./uninstall.sh` (with the same `--local` option) removes it. Use one way or the other, not both, or Claude sees the skill twice.

## Use

In Claude Code, from or near the folder with your recordings:

> Split last night's rehearsal in ~/Music/rehearsals/2026-10-08 into songs. It was all recorded on my Zoom.

Claude runs the scripts; you do the trimming and naming in the browser. `SKILL.md` describes the whole flow, and each script has `--help` if you want to run them yourself.

## Limits

This was built for one band's practice recordings and has not had much use beyond that.

- **Platform.** Written and used on macOS only. Other systems are untested; M4A input and output will not work there, as they rely on a tool that ships with macOS.
- **Input.** WAV, FLAC, MP3, M4A, AIFF and OGG files, and Audacity `.aup4` projects.
- **Kind of material.** Song detection expects music with gaps between songs: talking, tuning, silence. Songs that run straight into each other come out as one take, and a song with a silence of more than about 15 seconds in it comes out as two. Both can be fixed while trimming or with the options described in `SKILL.md`.
- **Several mics.** They are lined up by what they heard, so they need to have recorded the same room. Very short recordings (under ten minutes) can be matched to the wrong place.
- **Browser.** The trimming page has only been used in Chrome.
- **Loudness.** Songs are levelled by measurement, to about the loudness streaming services use, with a limiter. It is meant to make practice recordings comfortable to listen to, not to master them.

## License

MIT. See `LICENSE`.
