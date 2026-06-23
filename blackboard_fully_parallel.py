import argparse
import ast
import json
import queue
import re
import threading
import time
import uuid
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
SKIP_NAME_PARTS = (".png", ".jpg", ".jpeg", ".gif", "animation")
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


@dataclass(frozen=True)
class DownloadTask:
    url: str
    label: str
    directory: Path
    source: str


@dataclass(frozen=True)
class DownloadResult:
    task: DownloadTask
    status: str
    path: Path | None = None
    detail: str = ""


@dataclass
class Stats:
    found: int = 0
    downloaded: int = 0
    needs_browser: int = 0
    failed: int = 0
    seen_urls: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(message, flush=True)


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


def login(driver):
    if COOKIE_PATH.exists():
        driver.get(HOST_WITHOUT_REDIRECT)
        isloaded(driver)
        load_cookie(driver, COOKIE_PATH)
        driver.get(HOST)
        isloaded(driver)
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


def enqueue_task(download_queue, stats, url, label, directory, source):
    if not url or should_skip_name(label):
        return
    task = DownloadTask(url=url, label=validate_title(label), directory=directory, source=source)
    with stats.lock:
        if task.url in stats.seen_urls:
            return
        stats.seen_urls.add(task.url)
        stats.found += 1
        found = stats.found
    log(f"[found {found}] {task.directory / task.label}")
    download_queue.put(task)


def collect_video_links(download_queue, stats, driver, directory):
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
            enqueue_task(download_queue, stats, href, label, directory, "video-link")


def collect_from_table(download_queue, stats, table, base_dir):
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
                    enqueue_task(download_queue, stats, href, label, cell_dir, "table")


def collect_content(driver, download_queue, stats, directory, depth=1):
    directory.mkdir(parents=True, exist_ok=True)
    collect_video_links(download_queue, stats, driver, directory)
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
            enqueue_task(download_queue, stats, link.get_attribute("href"), link.text, directory, "file")

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
            collect_content(driver, download_queue, stats, directory / folder_name, depth + 1)
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
                enqueue_task(download_queue, stats, attachment.get_attribute("href"), attachment.text, item_dir, "attachment")

            for table in item.find_elements(By.XPATH, './../div[2]/div/table'):
                collect_from_table(download_queue, stats, table, item_dir)

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


def scan_course(course, cookies, root_dir, download_queue, stats, headless):
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
        collect_content(driver, download_queue, stats, course_dir)
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
    return parser.parse_args()


def main():
    args = parse_args()
    root_dir = Path(get_root_dir()).expanduser()
    login_driver = make_driver(headless=False)

    try:
        login(login_driver)
        courses = get_course_links(login_driver)
        if args.course:
            courses = [(name, href) for name, href in courses if args.course.lower() in name.lower()]
        cookies = login_driver.get_cookies()
    finally:
        login_driver.quit()

    if not courses:
        log("No matching courses found.")
        return

    download_queue = queue.Queue(maxsize=max(16, args.download_workers * 4))
    stats = Stats()
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

    scan_workers = max(1, args.scan_workers)
    log(
        f"Scanning {len(courses)} courses with {scan_workers} scanner(s); "
        f"downloading with {max(1, args.download_workers)} worker(s)."
    )

    with ThreadPoolExecutor(max_workers=scan_workers) as executor:
        futures = [
            executor.submit(
                scan_course,
                course,
                cookies,
                root_dir,
                download_queue,
                stats,
                not args.show_scanners,
            )
            for course in courses
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                log(f"[scan failed] {exc}")

    for _ in download_threads:
        download_queue.put(STOP)
    download_queue.join()
    for thread in download_threads:
        thread.join()

    log("\nSummary")
    log(f"Found: {stats.found}")
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
