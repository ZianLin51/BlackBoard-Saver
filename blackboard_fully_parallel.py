import argparse
import ast
import json
import queue
import re
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException, ElementNotInteractableException, NoSuchElementException
from selenium.webdriver.common.by import By

from config import get_email, get_password, get_root_dir


HOST = "https://bb.imperial.ac.uk/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1"
HOST_BASE = "https://bb.imperial.ac.uk/"
HOST_WITHOUT_REDIRECT = "https://bb.imperial.ac.uk/auth-saml/logout/"
COOKIE_PATH = Path("cookies.txt")
SKIP_NAME_PARTS = ("animation",)
STOP = object()
VIDEO_HINTS = (
    "video",
    "recording",
    "lecturecast",
    "panopto",
    "echo360",
    "stream",
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
)
KNOWN_FILE_EXTENSIONS = {
    ".7z",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".htm",
    ".html",
    ".jpeg",
    ".jpg",
    ".m",
    ".m4v",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".py",
    ".rar",
    ".txt",
    ".webm",
    ".xls",
    ".xlsx",
    ".zip",
}
CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "video/x-m4v": ".m4v",
}
DEFAULT_EXCLUDED_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png"}
DEFAULT_ALLOWED_EXTENSIONS = KNOWN_FILE_EXTENSIONS - DEFAULT_EXCLUDED_EXTENSIONS


@dataclass(frozen=True)
class DownloadTask:
    url: str
    label: str
    directory: Path
    source: str
    page_url: str = ""


@dataclass(frozen=True)
class DownloadCandidate:
    task: DownloadTask
    filename: str
    extension: str
    size_bytes: int | None
    content_type: str = ""
    resolved_url: str = ""
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DownloadResult:
    task: DownloadTask
    status: str
    path: Path | None = None
    detail: str = ""


@dataclass
class Stats:
    found: int = 0
    filtered: int = 0
    downloaded: int = 0
    needs_browser: int = 0
    failed: int = 0
    seen_urls: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


print_lock = threading.Lock()


@dataclass(frozen=True)
class FilterOptions:
    allowed_extensions: frozenset[str] = frozenset(DEFAULT_ALLOWED_EXTENSIONS)
    max_size_bytes: int | None = None
    keep_unknown_types: bool = True
    keep_unknown_size: bool = True


def log(message):
    with print_lock:
        print(message, flush=True)


def parse_extensions(value):
    extensions = set()
    for part in (value or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = f".{part}"
        extensions.add(part)
    return frozenset(extensions)


def parse_content_length(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_size(size_bytes):
    if size_bytes is None:
        return "unknown"
    units = ("B", "KB", "MB", "GB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def inspect_task(task, cookies):
    session = make_session(cookies)
    response = None
    try:
        try:
            response = session.head(task.url, allow_redirects=True, timeout=30)
            if response.status_code >= 400 or not response.headers:
                response.close()
                response = None
        except requests.RequestException:
            response = None

        if response is None:
            response = session.get(task.url, allow_redirects=True, stream=True, timeout=30)

        content_type = response.headers.get("content-type", "").lower()
        server_name = filename_from_response(response, task.label)
        filename = final_name(task.label, server_name, content_type)
        extension = Path(filename).suffix.lower() or extension_from_content_type(content_type)
        size_bytes = parse_content_length(response.headers.get("content-length"))
        resolved_url = response.url
    except Exception:
        filename = validate_title(task.label)
        extension = Path(filename).suffix.lower()
        size_bytes = None
        content_type = ""
        resolved_url = task.url
    finally:
        if response is not None:
            response.close()
        session.close()

    return DownloadCandidate(
        task=task,
        filename=filename,
        extension=extension,
        size_bytes=size_bytes,
        content_type=content_type,
        resolved_url=resolved_url,
    )


def filter_candidate(candidate, filters):
    reasons = []
    extension = candidate.extension.lower()

    if extension:
        if extension not in filters.allowed_extensions:
            reasons.append(f"type {extension} not selected")
    elif not filters.keep_unknown_types:
        reasons.append("file type unknown")

    if filters.max_size_bytes is not None:
        if candidate.size_bytes is None:
            if not filters.keep_unknown_size:
                reasons.append("file size unknown")
        elif candidate.size_bytes > filters.max_size_bytes:
            reasons.append(f"larger than {format_size(filters.max_size_bytes)}")

    if not reasons:
        return candidate
    return DownloadCandidate(
        task=candidate.task,
        filename=candidate.filename,
        extension=candidate.extension,
        size_bytes=candidate.size_bytes,
        content_type=candidate.content_type,
        resolved_url=candidate.resolved_url,
        reasons=tuple(reasons),
    )


class TaskCollector:
    def __init__(self, cookies, filters, stats, download_queue):
        self.cookies = cookies
        self.filters = filters
        self.stats = stats
        self.download_queue = download_queue
        self.accepted = []
        self.rejected = []
        self.lock = threading.Lock()

    def add(self, url, label, directory, source, page_url):
        if not url or should_skip_name(label):
            return

        task = DownloadTask(
            url=url,
            label=validate_title(label),
            directory=directory,
            source=source,
            page_url=page_url or "",
        )
        with self.stats.lock:
            if task.url in self.stats.seen_urls:
                return
            self.stats.seen_urls.add(task.url)
            self.stats.found += 1
            found = self.stats.found

        candidate = filter_candidate(inspect_task(task, self.cookies), self.filters)
        with self.lock:
            if candidate.reasons:
                self.rejected.append(candidate)
                with self.stats.lock:
                    self.stats.filtered += 1
                log(
                    f"[filtered {found}] {task.directory / candidate.filename} "
                    f"({'; '.join(candidate.reasons)})"
                )
            else:
                self.accepted.append(candidate)
                log(f"[found {found}] {task.directory / candidate.filename}")
                self.download_queue.put(candidate.task)


def isloaded(driver, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if driver.execute_script("return document.readyState == 'complete';"):
            return
        time.sleep(0.25)
    raise TimeoutError("Timed out waiting for page load")


def read_cookie_file(path):
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return ast.literal_eval(raw)


def save_cookie(driver, path):
    path.write_text(json.dumps(driver.get_cookies(), indent=2), encoding="utf-8")


def load_cookie(driver, path):
    add_cookies(driver, read_cookie_file(path))


def add_cookies(driver, cookies):
    for cookie in cookies:
        cookie = dict(cookie)
        cookie.pop("sameSite", None)
        try:
            driver.add_cookie(cookie)
        except Exception as exc:
            log(f"Skipped cookie {cookie.get('name')}: {exc}")


def validate_title(title):
    title = re.sub(r"[\/\\\:\*\?\"\<\>\|]", "_", title.strip())
    title = re.sub(r"\s+", " ", title)
    return title or "untitled"


def should_skip_name(name):
    lower_name = name.lower()
    return any(part in lower_name for part in SKIP_NAME_PARTS)


def looks_like_video(text, href):
    if "#contextMenu" in href or href.endswith("#close"):
        return False
    if "/webapps/blackboard/content/listContent.jsp" in href:
        return False

    value = f"{text} {href}".lower()
    if text.strip().lower() == "video" and "bbcswebdav" not in href.lower():
        return False

    return any(hint in value for hint in VIDEO_HINTS)


def xpath_literal(value):
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ', \'"\', '.join(f'"{part}"' for part in parts) + ")"


def click_when_visible(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.2)
    try:
        element.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        driver.execute_script("arguments[0].click();", element)


def find_visible_link_by_text(driver, texts):
    for text in texts:
        candidates = driver.find_elements(
            By.XPATH,
            f"//*[normalize-space()={xpath_literal(text)}]/ancestor-or-self::a[1]",
        )
        for candidate in candidates:
            if candidate.is_displayed() and candidate.is_enabled():
                return candidate
    return None


def show_filter_options_ui(defaults):
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as exc:
        log(f"Could not open filter UI ({exc}); continuing with command-line/default filters.")
        return defaults

    result = {"options": None}
    root = tk.Tk()
    root.title("Blackboard Saver filters")
    root.geometry("760x620")
    root.minsize(680, 520)

    main = ttk.Frame(root, padding=16)
    main.pack(fill="both", expand=True)

    ttk.Label(main, text="Choose what to download before scanning").pack(anchor="w")

    type_frame = ttk.LabelFrame(main, text="File types", padding=12)
    type_frame.pack(fill="both", expand=True, pady=(12, 8))

    type_vars = {}
    extensions = sorted(KNOWN_FILE_EXTENSIONS)
    for index, extension in enumerate(extensions):
        var = tk.BooleanVar(value=extension in defaults.allowed_extensions)
        type_vars[extension] = var
        row = index // 4
        column = index % 4
        ttk.Checkbutton(type_frame, text=extension, variable=var).grid(
            row=row,
            column=column,
            sticky="w",
            padx=(0, 24),
            pady=3,
        )

    def set_all(value):
        for var in type_vars.values():
            var.set(value)

    button_row = ttk.Frame(main)
    button_row.pack(fill="x", pady=(0, 8))
    ttk.Button(button_row, text="Select all", command=lambda: set_all(True)).pack(side="left")
    ttk.Button(button_row, text="Select none", command=lambda: set_all(False)).pack(side="left", padx=(8, 0))
    ttk.Button(
        button_row,
        text="Default",
        command=lambda: [var.set(ext in DEFAULT_ALLOWED_EXTENSIONS) for ext, var in type_vars.items()],
    ).pack(side="left", padx=(8, 0))

    limits_frame = ttk.LabelFrame(main, text="Limits", padding=12)
    limits_frame.pack(fill="x")

    ttk.Label(limits_frame, text="Maximum file size (MB, blank for no limit)").grid(row=0, column=0, sticky="w")
    max_size_var = tk.StringVar(
        value="" if defaults.max_size_bytes is None else f"{defaults.max_size_bytes / (1024 * 1024):g}"
    )
    ttk.Entry(limits_frame, textvariable=max_size_var, width=16).grid(row=0, column=1, sticky="w", padx=(12, 0))

    keep_unknown_types_var = tk.BooleanVar(value=defaults.keep_unknown_types)
    keep_unknown_size_var = tk.BooleanVar(value=defaults.keep_unknown_size)
    ttk.Checkbutton(
        limits_frame,
        text="Keep files with unknown type",
        variable=keep_unknown_types_var,
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
    ttk.Checkbutton(
        limits_frame,
        text="Keep files with unknown size",
        variable=keep_unknown_size_var,
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

    action_row = ttk.Frame(main)
    action_row.pack(fill="x", pady=(14, 0))

    def start():
        raw_max_size = max_size_var.get().strip()
        max_size_bytes = None
        if raw_max_size:
            try:
                max_size_mb = float(raw_max_size)
                if max_size_mb < 0:
                    raise ValueError
                max_size_bytes = int(max_size_mb * 1024 * 1024)
            except ValueError:
                messagebox.showerror("Invalid size", "Maximum file size must be a positive number or blank.")
                return

        selected = frozenset(ext for ext, var in type_vars.items() if var.get())
        result["options"] = FilterOptions(
            allowed_extensions=selected,
            max_size_bytes=max_size_bytes,
            keep_unknown_types=keep_unknown_types_var.get(),
            keep_unknown_size=keep_unknown_size_var.get(),
        )
        root.destroy()

    def cancel():
        root.destroy()

    ttk.Button(action_row, text="Start scanning", command=start).pack(side="right")
    ttk.Button(action_row, text="Cancel", command=cancel).pack(side="right", padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()
    return result["options"]


def review_filtered_candidates_ui(candidates):
    if not candidates:
        return []

    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        log(f"Could not open review UI ({exc}); filtered files will be skipped.")
        return []

    selected = {"candidates": []}
    root = tk.Tk()
    root.title("Review filtered Blackboard files")
    root.geometry("1120x720")
    root.minsize(900, 520)

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)

    ttk.Label(
        main,
        text="Filtered files are unchecked. Tick any file you still want to download.",
    ).pack(anchor="w")

    toolbar = ttk.Frame(main)
    toolbar.pack(fill="x", pady=(8, 8))

    canvas = tk.Canvas(main, highlightthickness=0)
    scrollbar = ttk.Scrollbar(main, orient="vertical", command=canvas.yview)
    rows_frame = ttk.Frame(canvas)
    rows_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas_window = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    def resize_canvas(event):
        canvas.itemconfigure(canvas_window, width=event.width)

    canvas.bind("<Configure>", resize_canvas)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    def on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind_all("<MouseWheel>", on_mousewheel)

    headers = ("Keep", "File", "Type", "Size", "Reason", "Blackboard page")
    widths = (7, 45, 9, 12, 30, 18)
    for column, (header, width) in enumerate(zip(headers, widths)):
        ttk.Label(rows_frame, text=header, width=width).grid(row=0, column=column, sticky="w", padx=4, pady=(0, 6))

    vars_by_candidate = []
    for row, candidate in enumerate(candidates, start=1):
        keep_var = tk.BooleanVar(value=False)
        vars_by_candidate.append((keep_var, candidate))

        ttk.Checkbutton(rows_frame, variable=keep_var).grid(row=row, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(rows_frame, text=candidate.filename, width=45).grid(row=row, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(rows_frame, text=candidate.extension or "unknown", width=9).grid(
            row=row,
            column=2,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Label(rows_frame, text=format_size(candidate.size_bytes), width=12).grid(
            row=row,
            column=3,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Label(rows_frame, text="; ".join(candidate.reasons), width=30).grid(
            row=row,
            column=4,
            sticky="w",
            padx=4,
            pady=2,
        )

        page_url = candidate.task.page_url or candidate.task.url
        ttk.Button(
            rows_frame,
            text="Open page",
            command=lambda url=page_url: webbrowser.open(url),
            width=14,
        ).grid(row=row, column=5, sticky="w", padx=4, pady=2)

    def set_all(value):
        for var, _candidate in vars_by_candidate:
            var.set(value)

    def continue_download():
        selected["candidates"] = [candidate for var, candidate in vars_by_candidate if var.get()]
        root.destroy()

    ttk.Button(toolbar, text="Keep all", command=lambda: set_all(True)).pack(side="left")
    ttk.Button(toolbar, text="Keep none", command=lambda: set_all(False)).pack(side="left", padx=(8, 0))
    ttk.Label(toolbar, text=f"{len(candidates)} filtered file(s)").pack(side="left", padx=(16, 0))
    ttk.Button(toolbar, text="Continue", command=continue_download).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", continue_download)
    root.mainloop()
    return selected["candidates"]


def wait_for_login_confirmation_ui():
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        log(f"Could not open login confirmation UI ({exc}); falling back to terminal confirmation.")
        input("Finish any MFA/browser login prompts, then press Enter here...")
        return True

    result = {"confirmed": False}
    root = tk.Tk()
    root.title("Blackboard login")
    root.geometry("520x180")
    root.minsize(460, 160)
    root.attributes("-topmost", True)

    main = ttk.Frame(root, padding=18)
    main.pack(fill="both", expand=True)

    ttk.Label(main, text="Finish logging in to Blackboard in the browser.").pack(anchor="w")
    ttk.Label(main, text="After the Blackboard course page is visible, click Confirm to start scanning.").pack(
        anchor="w",
        pady=(8, 0),
    )

    actions = ttk.Frame(main)
    actions.pack(fill="x", pady=(22, 0))

    def confirm():
        result["confirmed"] = True
        root.destroy()

    def cancel():
        root.destroy()

    ttk.Button(actions, text="Confirm and scan", command=confirm).pack(side="right")
    ttk.Button(actions, text="Cancel", command=cancel).pack(side="right", padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.after(800, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return result["confirmed"]


def make_driver(headless=False):
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_experimental_option(
        "prefs",
        {
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True,
        },
    )
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)
    if not headless:
        driver.maximize_window()
    driver.implicitly_wait(2)
    return driver


def login(driver, use_ui=True):
    if COOKIE_PATH.exists():
        driver.get(HOST_WITHOUT_REDIRECT)
        isloaded(driver)
        load_cookie(driver, COOKIE_PATH)
        driver.get(HOST)
        isloaded(driver)
        return

    if use_ui:
        driver.get(HOST)
        isloaded(driver)
        if not wait_for_login_confirmation_ui():
            raise RuntimeError("Login cancelled.")
        driver.get(HOST)
        isloaded(driver)
        save_cookie(driver, COOKIE_PATH)
        return

    email = get_email()
    password = get_password()
    driver.get(HOST)
    isloaded(driver)
    driver.find_element(By.XPATH, '//*[@id="i0116"]').send_keys(email)
    driver.find_element(By.XPATH, '//*[@id="idSIButton9"]').click()
    time.sleep(2)
    driver.find_element(By.XPATH, '//*[@id="i0118"]').send_keys(password)
    time.sleep(0.5)
    driver.find_element(By.XPATH, '//*[@id="idSIButton9"]').click()
    time.sleep(0.5)
    input("Finish any MFA/browser login prompts, then press Enter here...")
    driver.get(HOST)
    isloaded(driver)
    save_cookie(driver, COOKIE_PATH)


def get_course_links(driver):
    courses = []
    links = driver.find_elements(By.XPATH, '//*[@id="_4_1termCourses_noterm"]/ul/*/a')
    for link in links:
        name = validate_title(link.text)
        href = link.get_attribute("href")
        if name and href:
            courses.append((name, href))
    return courses


def prepare_scanner_driver(cookies, headless):
    driver = make_driver(headless=headless)
    driver.execute_cdp_cmd("Network.enable", {})
    for cookie in cookies:
        params = {
            "name": cookie["name"],
            "value": cookie["value"],
            "domain": cookie.get("domain", ".bb.imperial.ac.uk"),
            "path": cookie.get("path", "/"),
            "secure": bool(cookie.get("secure", True)),
            "httpOnly": bool(cookie.get("httpOnly", False)),
        }
        if cookie.get("expiry"):
            params["expires"] = int(cookie["expiry"])
        driver.execute_cdp_cmd("Network.setCookie", params)
    driver.get(HOST)
    isloaded(driver)
    return driver


def enqueue_task(collector, url, label, directory, source, page_url):
    collector.add(url, label, directory, source, page_url)


def collect_video_links(collector, driver, directory, page_url):
    links = driver.execute_script(
        """
        return Array.from(document.querySelectorAll('a[href]')).map((link) => ({
            href: link.href,
            text: (link.innerText || link.textContent || link.getAttribute('title') || link.href || '').trim()
        }));
        """
    )
    for link in links:
        label = validate_title(link.get("text") or "video")
        href = link.get("href")
        if href and looks_like_video(label, href):
            enqueue_task(collector, href, label, directory, "video-link", page_url)


def collect_from_table(collector, table, base_dir, page_url):
    rows = table.find_elements(By.XPATH, "./tbody/tr")
    columns = []
    for row_index, row in enumerate(rows):
        cells = row.find_elements(By.XPATH, "./td")
        if row_index == 0:
            columns = [validate_title(cell.text) for cell in cells]
            continue
        if not cells:
            continue

        row_dir = base_dir / validate_title(cells[0].text)
        for cell_index, cell in enumerate(cells[1:], start=1):
            column_name = columns[cell_index] if cell_index < len(columns) else f"Column {cell_index}"
            cell_dir = row_dir / validate_title(column_name)
            for file_link in cell.find_elements(By.XPATH, ".//a[@href]"):
                href = file_link.get_attribute("href")
                if href and "ant-x" not in href:
                    label = file_link.text or Path(urlparse(href).path).name
                    enqueue_task(collector, href, label, cell_dir, "table", page_url)


def collect_content(driver, collector, directory, depth=1):
    directory.mkdir(parents=True, exist_ok=True)
    page_url = driver.current_url
    collect_video_links(collector, driver, directory, page_url)
    items = driver.find_elements(By.CLASS_NAME, "item_icon")
    log(f"{'  ' * (depth - 1)}Scanning {len(items)} items in {directory}")

    for index in range(len(items)):
        items = driver.find_elements(By.CLASS_NAME, "item_icon")
        if index >= len(items):
            break

        item = items[index]
        item_type = (item.get_attribute("alt") or "").strip()
        item_type_lower = item_type.lower()

        if item_type_lower == "file":
            try:
                link = item.find_element(By.XPATH, './../*/h3/a')
            except NoSuchElementException:
                log(f"{'  ' * depth}Could not find file link for item {index + 1}")
                continue
            enqueue_task(collector, link.get_attribute("href"), link.text, directory, "file", page_url)

        elif item_type_lower == "content folder":
            try:
                folder_heading = item.find_element(By.XPATH, './../*/h3')
            except NoSuchElementException:
                log(f"{'  ' * depth}Could not find folder heading for item {index + 1}")
                continue

            folder_name = validate_title(folder_heading.text)
            log(f"{'  ' * depth}{folder_name}/")
            folder_link = folder_heading
            try:
                folder_link = folder_heading.find_element(By.XPATH, ".//a")
            except NoSuchElementException:
                pass

            click_when_visible(driver, folder_link)
            isloaded(driver)
            collect_content(driver, collector, directory / folder_name, depth + 1)
            driver.back()
            isloaded(driver)

        elif item_type_lower == "item":
            try:
                item_name = validate_title(item.find_element(By.XPATH, './../*/h3').text)
            except NoSuchElementException:
                item_name = f"Item {index + 1}"

            item_dir = directory / item_name
            item_dir.mkdir(parents=True, exist_ok=True)
            log(f"{'  ' * depth}{item_name}/")

            for attachment in item.find_elements(By.XPATH, './../div[2]/div[1]/div[2]/ul/*/a[1]'):
                enqueue_task(
                    collector,
                    attachment.get_attribute("href"),
                    attachment.text,
                    item_dir,
                    "attachment",
                    page_url,
                )

            for table in item.find_elements(By.XPATH, './../div[2]/div/table'):
                collect_from_table(collector, table, item_dir, page_url)

        elif item_type_lower == "linked item":
            continue

        else:
            try:
                heading = item.find_element(By.XPATH, './../*/h3').text
            except NoSuchElementException:
                heading = f"item {index + 1}"
            log(f"{'  ' * depth}Needs review: {heading} ({item_type or 'unknown type'})")


def make_session(cookies):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 Blackboard fully parallel downloader",
            "Referer": HOST,
        }
    )
    for cookie in cookies:
        kwargs = {"path": cookie.get("path", "/")}
        if cookie.get("domain"):
            kwargs["domain"] = cookie["domain"]
        session.cookies.set(cookie["name"], cookie["value"], **kwargs)
    return session


def filename_from_response(response, fallback):
    content_disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.I)
    if match:
        return validate_title(unquote(match.group(1)))

    match = re.search(r'filename="?([^";]+)"?', content_disposition, flags=re.I)
    if match:
        return validate_title(match.group(1))

    path_name = Path(unquote(urlparse(response.url).path)).name
    return validate_title(path_name or fallback)


def known_file_suffix(name):
    return Path(name).suffix.lower() in KNOWN_FILE_EXTENSIONS


def extension_from_content_type(content_type):
    media_type = content_type.split(";", 1)[0].strip().lower()
    return CONTENT_TYPE_EXTENSIONS.get(media_type, "")


def final_name(label, server_name, content_type=""):
    if known_file_suffix(label):
        return validate_title(label)

    server_suffix = Path(server_name).suffix
    if server_suffix.lower() in KNOWN_FILE_EXTENSIONS:
        return validate_title(label + server_suffix)

    content_type_suffix = extension_from_content_type(content_type)
    if content_type_suffix:
        return validate_title(label + content_type_suffix)

    return validate_title(label)


def reserve_path(directory, filename, lock, reserved):
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1

    with lock:
        while candidate.exists() or str(candidate).lower() in reserved:
            candidate = directory / f"{stem} ({counter}){suffix}"
            counter += 1
        reserved.add(str(candidate).lower())
        return candidate


def download_one(task, cookies, lock, reserved):
    session = make_session(cookies)
    try:
        response = session.get(task.url, allow_redirects=True, stream=True, timeout=90)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        has_disposition = "content-disposition" in response.headers
        if "text/html" in content_type and not has_disposition:
            return DownloadResult(
                task=task,
                status="needs_browser",
                detail=f"URL returned HTML instead of a file: {response.url}",
            )

        server_name = filename_from_response(response, task.label)
        target = reserve_path(task.directory, final_name(task.label, server_name, content_type), lock, reserved)
        temp_path = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")

        with temp_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    output.write(chunk)
        temp_path.replace(target)
        return DownloadResult(task=task, status="downloaded", path=target)
    except Exception as exc:
        return DownloadResult(task=task, status="failed", detail=str(exc))
    finally:
        session.close()


def download_worker(worker_id, download_queue, cookies, path_lock, reserved, stats, results, dry_run):
    while True:
        task = download_queue.get()
        try:
            if task is STOP:
                return

            if dry_run:
                log(f"[dry-run worker {worker_id}] {task.directory / task.label} <- {task.url}")
                continue

            result = download_one(task, cookies, path_lock, reserved)
            results.append(result)
            with stats.lock:
                if result.status == "downloaded":
                    stats.downloaded += 1
                elif result.status == "needs_browser":
                    stats.needs_browser += 1
                else:
                    stats.failed += 1
                done = stats.downloaded + stats.needs_browser + stats.failed

            if result.status == "downloaded":
                log(f"[done {done}] saved {result.path}")
            else:
                log(f"[done {done}] {result.status}: {result.task.label} - {result.detail}")
        finally:
            download_queue.task_done()


def scan_course(course, cookies, root_dir, collector, headless):
    course_name, course_url = course
    driver = prepare_scanner_driver(cookies, headless=headless)
    try:
        log(f"\n[scan] Course: {course_name}")
        course_dir = root_dir / course_name
        driver.get(course_url)
        isloaded(driver)
        learning_material = find_visible_link_by_text(
            driver,
            ["Learning Materials", "Learning materials", "Course Content", "Content"],
        )
        if learning_material is None:
            log(f"[scan] No learning material link found for {course_name}")
            return
        click_when_visible(driver, learning_material)
        isloaded(driver)
        collect_content(driver, collector, course_dir)
    finally:
        driver.quit()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scan Blackboard with multiple browser workers while download workers save files immediately."
    )
    parser.add_argument("--scan-workers", type=int, default=8, help="Number of parallel Selenium scanner browsers.")
    parser.add_argument("--download-workers", type=int, default=16, help="Number of parallel HTTP download workers.")
    parser.add_argument("--course", help="Only download courses whose name contains this text.")
    parser.add_argument("--dry-run", action="store_true", help="Scan and print tasks without downloading.")
    parser.add_argument("--show-scanners", action="store_true", help="Show scanner browser windows instead of headless scanners.")
    parser.add_argument("--no-ui", action="store_true", help="Skip the filter and review windows.")
    parser.add_argument("--types", help="Comma-separated extensions to download, for example pdf,docx,pptx.")
    parser.add_argument("--max-size-mb", type=float, help="Maximum file size to download automatically.")
    parser.add_argument("--exclude-unknown-types", action="store_true", help="Review files whose type cannot be detected.")
    parser.add_argument("--exclude-unknown-size", action="store_true", help="Review files whose size cannot be detected.")
    return parser.parse_args()


def filter_options_from_args(args):
    allowed_extensions = parse_extensions(args.types) if args.types else frozenset(DEFAULT_ALLOWED_EXTENSIONS)
    max_size_bytes = None
    if args.max_size_mb is not None:
        max_size_bytes = max(0, int(args.max_size_mb * 1024 * 1024))
    return FilterOptions(
        allowed_extensions=allowed_extensions,
        max_size_bytes=max_size_bytes,
        keep_unknown_types=not args.exclude_unknown_types,
        keep_unknown_size=not args.exclude_unknown_size,
    )


def main():
    args = parse_args()
    filters = filter_options_from_args(args)
    if not args.no_ui:
        filters = show_filter_options_ui(filters)
        if filters is None:
            log("Cancelled.")
            return

    root_dir = Path(get_root_dir()).expanduser()
    login_driver = make_driver(headless=False)

    try:
        login(login_driver, use_ui=not args.no_ui)
        courses = get_course_links(login_driver)
        if not courses and not args.no_ui:
            log("No courses were visible after loading cookies; asking for a fresh browser login.")
            if not wait_for_login_confirmation_ui():
                raise RuntimeError("Login cancelled.")
            login_driver.get(HOST)
            isloaded(login_driver)
            save_cookie(login_driver, COOKIE_PATH)
            courses = get_course_links(login_driver)
        if args.course:
            courses = [(name, href) for name, href in courses if args.course.lower() in name.lower()]
        cookies = login_driver.get_cookies()
    except RuntimeError as exc:
        log(str(exc))
        return
    finally:
        login_driver.quit()

    if not courses:
        log("No matching courses found.")
        return

    stats = Stats()
    download_queue = queue.Queue(maxsize=max(16, args.download_workers * 4))
    results = []
    path_lock = threading.Lock()
    reserved = set()

    download_threads = []
    for worker_id in range(1, max(1, args.download_workers) + 1):
        thread = threading.Thread(
            target=download_worker,
            args=(worker_id, download_queue, cookies, path_lock, reserved, stats, results, args.dry_run),
            daemon=True,
        )
        thread.start()
        download_threads.append(thread)

    collector = TaskCollector(cookies, filters, stats, download_queue)

    scan_workers = max(1, args.scan_workers)
    log(
        f"Scanning {len(courses)} courses with {scan_workers} scanner(s); "
        f"matching files will download immediately."
    )

    with ThreadPoolExecutor(max_workers=scan_workers) as executor:
        futures = [
            executor.submit(
                scan_course,
                course,
                cookies,
                root_dir,
                collector,
                not args.show_scanners,
            )
            for course in courses
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                log(f"[scan failed] {exc}")

    selected_rejected = []
    if collector.rejected and not args.no_ui:
        selected_rejected = review_filtered_candidates_ui(collector.rejected)

    for candidate in selected_rejected:
        download_queue.put(candidate.task)

    log(
        f"\nAccepted immediately: {len(collector.accepted)} file(s). "
        f"Manually kept: {len(selected_rejected)}. "
        f"Filtered out: {len(collector.rejected) - len(selected_rejected)}"
    )

    for _ in download_threads:
        download_queue.put(STOP)
    download_queue.join()
    for thread in download_threads:
        thread.join()

    log("\nSummary")
    log(f"Found: {stats.found}")
    log(f"Filtered for review: {stats.filtered}")
    log(f"Downloaded: {stats.downloaded}")
    log(f"Needs browser/manual handling: {stats.needs_browser}")
    log(f"Failed: {stats.failed}")

    needs_browser = [result for result in results if result.status == "needs_browser"]
    failed = [result for result in results if result.status == "failed"]

    if needs_browser:
        log("\nThese links probably require an extra Blackboard click or a new scraper rule:")
        for result in needs_browser:
            log(f"- {result.task.label}: {result.task.url}")

    if failed:
        log("\nFailed downloads:")
        for result in failed:
            log(f"- {result.task.label}: {result.detail}")


if __name__ == "__main__":
    main()
