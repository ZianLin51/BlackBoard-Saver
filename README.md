# Blackboard Saver

Blackboard Saver is a Selenium-based downloader for Imperial College Blackboard.
It signs in through a real browser session, scans your enrolled courses, and saves course files into a local folder structure that mirrors the Blackboard course layout.

The parallel downloader is designed for large Blackboard accounts: multiple browser workers scan courses while multiple HTTP workers download matching files in the background.

## Features

- Browser-based Blackboard login with MFA support.
- Course scanning for files, attachments, tables, folders, and common video links.
- Parallel scanning and downloading for faster bulk exports.
- Visual filter UI before scanning.
- File type and maximum file size filters.
- Review window for filtered-out files, including a link back to the Blackboard page where each item was found.
- Automatic local folder organization by course and content folder.
- Cookie reuse between runs via `cookies.txt`.
- Command-line options for automation or headless-style runs.

## Requirements

- Python 3.10 or newer.
- Google Chrome.
- ChromeDriver compatible with your installed Chrome version.
- Access to Imperial College Blackboard.

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Current Python package requirements:

```text
selenium==4.45.0
requests==2.34.2
```

## Quick Start

Run the fully parallel downloader:

```bash
python blackboard_fully_parallel.py
```

Default UI workflow:

1. Choose file filters in the startup window.
2. Click `Start scanning`.
3. Complete Blackboard login and MFA in the browser.
4. Click `Confirm and scan` in the login confirmation window.
5. Files that match the filters begin downloading as soon as they are found.
6. After scanning finishes, review filtered-out files and manually keep any you still want.

Downloaded files are saved to:

```text
~/Downloads/Blackboard
```

You can change the output folder when prompted, through environment variables, or with a local `key.py` file.

## Configuration

The downloader reads configuration in this order:

1. Environment variables.
2. A local `key.py` file.
3. Interactive prompts.

Supported environment variables:

```bash
BLACKBOARD_EMAIL="your.name@example.com"
BLACKBOARD_PASSWORD="your-password"
BLACKBOARD_ROOT_DIR="/path/to/download/folder"
```

Optional `key.py` example:

```python
EMAIL = "your.name@example.com"
PASSWORD = "your-password"
ROOT_DIR = r"C:\Users\you\Downloads\Blackboard"
```

Do not commit `key.py`, cookies, or downloaded course material.

## Contributing

The maintainers are very lazy. If you fork this project, please do not expect us to review your commits.

## Command-Line Usage

Download only courses whose names contain a keyword:

```bash
python blackboard_fully_parallel.py --course "Machine Learning"
```

Run a dry scan without saving files:

```bash
python blackboard_fully_parallel.py --dry-run
```

Show scanner browser windows instead of running scanner browsers headlessly:

```bash
python blackboard_fully_parallel.py --show-scanners
```

Skip the UI and use command-line filters:

```bash
python blackboard_fully_parallel.py --no-ui --types pdf,docx,pptx --max-size-mb 200
```

Review files when their type or size cannot be detected:

```bash
python blackboard_fully_parallel.py --exclude-unknown-types --exclude-unknown-size
```

Tune parallelism:

```bash
python blackboard_fully_parallel.py --scan-workers 4 --download-workers 12
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--scan-workers` | `8` | Number of parallel Selenium browser scanners. |
| `--download-workers` | `16` | Number of parallel HTTP download workers. |
| `--course` | all courses | Only scan courses whose name contains this text. |
| `--dry-run` | off | Print download tasks without writing files. |
| `--show-scanners` | off | Show scanner browser windows instead of headless scanners. |
| `--no-ui` | off | Skip the filter and review windows. |
| `--types` | common document/archive/media types except small images | Comma-separated file extensions to download automatically. |
| `--max-size-mb` | unlimited | Maximum file size to download automatically. |
| `--exclude-unknown-types` | off | Put unknown file types into the review list. |
| `--exclude-unknown-size` | off | Put unknown file sizes into the review list. |

## Output Layout

Blackboard Saver creates folders based on course names and Blackboard content structure:

```text
Blackboard/
  ELEC60021 - Mathematics for Signals and Systems 2024-2025/
    Lecture Slides/
      Slides Session 1.pdf
      Slides Session 2.pdf
    Past Exam Papers/
      Exam paper 2023.pdf
```

If a filename already exists, the downloader appends a counter such as `(1)` to avoid overwriting files.

## Notes and Limitations

- This project is tailored to Imperial College Blackboard URLs and page structure. (may be adaptable to other Blackboard instances with selector and URL changes, not tested, try it if you really need to)
- Some Blackboard pages return HTML instead of direct file downloads. These are reported as `Needs browser/manual handling`.
- Blackboard may change its DOM structure over time. If scanning misses a section, use `--show-scanners` and inspect the page being scanned.
- The script stores reusable session cookies in `cookies.txt`. Delete that file if you need to force a fresh login.
- Very large Blackboard accounts may take a while to scan because each discovered file is inspected for metadata before filtering.

## Development

Useful checks before committing:

```bash
python -m py_compile blackboard_fully_parallel.py
git diff --check
```

The main implementation lives in:

- `blackboard_fully_parallel.py`: UI, login, scanning, filtering, and downloading.
- `config.py`: environment variable, `key.py`, and prompt-based configuration helpers.
