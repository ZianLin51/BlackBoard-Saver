# Blackboard Saver

Blackboard Saver helps you download course materials from Imperial College Blackboard in bulk.
It opens Blackboard in a Google browser, lets you choose what kinds of files you want, and saves everything into tidy course folders on your computer.

## Features

- Download course files from Blackboard without clicking through every page by hand.
- Choose which file types to download before scanning starts.
- Skip files above a maximum size you choose.
- Review skipped files later and manually keep anything important.
- View the original Blackboard page for any skipped file.
- Keep downloaded files organized by course and folder.
- Narrow the run to one course when you do not need everything.

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

Run the downloader:

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

You can change the output folder when prompted.

## Login

The normal UI flow does not require you to configure your Blackboard username or password in this project. On the first run, the downloader opens Blackboard in Chrome. Sign in normally, finish MFA if required, then click `Confirm and scan` in the small confirmation window.

After a successful login, future runs will usually be able to continue without asking you to sign in again.

If login starts behaving strangely, remove the saved login file and run the script again for a fresh browser login.

The script may still ask for a download folder in the terminal. Press Enter to use the default path, or type another folder path.

Do not commit saved login files or downloaded course material.

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

Adjust how much work the downloader does at once:

```bash
python blackboard_fully_parallel.py --scan-workers 4 --download-workers 12
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--scan-workers` | `8` | Number of course pages to scan at once. |
| `--download-workers` | `16` | Number of files to download at once. |
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

- This project is made for Imperial College Blackboard. Other schools may work after code changes, but that is not tested.
- Some Blackboard pages return HTML instead of direct file downloads. These are reported as `Needs browser/manual handling`.
- Blackboard may change over time. If scanning misses a section, use `--show-scanners` to see what the browser is doing.
- Very large Blackboard accounts may take a while to scan.

## Development

Useful checks before committing:

```bash
python -m py_compile blackboard_fully_parallel.py
git diff --check
```

The main implementation lives in:

- `blackboard_fully_parallel.py`: UI, login, scanning, filtering, and downloading.
- `config.py`: download folder and fallback prompt helpers.
