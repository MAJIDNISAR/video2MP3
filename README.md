# Video to MP3 Converter

A desktop application for batch-converting MP4 video files to MP3 audio, built with Python and Tkinter.

## Features

- **Batch conversion** -- add individual files or entire folders of MP4s
- **Multi-threaded** -- parallel conversion across all CPU cores with configurable thread count
- **Resilient** -- automatic retry (up to 3 attempts) on transient errors; immediate skip on unrecoverable errors (corrupt files, missing files)
- **Non-ASCII & long filename support** -- handles Unicode filenames (Hindi, Arabic, CJK, etc.), single quotes, and long names via Unicode normalization (NFC/NFD) and temporary symlinks
- **Skip & cancel** -- skip individual files mid-conversion or cancel the entire batch
- **Per-file progress** -- live progress percentage for each file and overall batch progress bar
- **Inline error display** -- errors shown per-file in the treeview table
- **Collapsible log panel** -- dark terminal-style log with color-coded entries (start, progress, retry, error, done)
- **Copyable logs** -- select text, Cmd+C, or right-click for Copy Selection / Copy All / Select All
- **Output location** -- MP3 files are written next to each source MP4

## Requirements

- Python 3.9+
- tkinter (included with most Python installs; on macOS via Homebrew: `brew install python-tk@3.x`)
- [moviepy](https://pypi.org/project/moviepy/) >= 1.0.3
- [Pillow](https://pypi.org/project/Pillow/) >= 9.0.0
- [proglog](https://pypi.org/project/proglog/)

## Installation

```bash
# Clone the repository
git clone git@github.com:MAJIDNISAR/video2MP3.git
cd video2MP3

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
source .venv/bin/activate
python mp4_to_mp3_converter.py
```

1. Click **Add Files** or **Add Folder** to queue MP4 files
2. Adjust the **Threads** count if needed (defaults to detected CPU cores)
3. Click **Convert** to start batch conversion
4. Use **Skip Selected** to skip slow/stuck files or **Cancel All** to abort
5. Toggle **Show Log** to see detailed activity log

## How It Works

- Files are dispatched to a `ThreadPoolExecutor` for parallel conversion
- Each file is converted using moviepy's `VideoFileClip.audio.write_audiofile()`
- Filenames with non-ASCII characters or quotes are handled by creating temporary symlinks with ASCII-safe names
- Corrupt files (e.g., missing moov atom) are detected and failed immediately without wasting retries
- A message queue bridges the worker threads and the Tkinter UI for thread-safe progress updates

## License

MIT
