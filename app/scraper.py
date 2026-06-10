# /app/scraper.py
from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Iterator, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()


def _dbg(msg: str) -> None:
    if os.getenv("DEBUG_STEPS", "").lower() == "true":
        print(f"[debug] {msg}")


def _to_ms_env(name: str, default_ms: int) -> int:
    try:
        return int(float(os.getenv(name, str(default_ms))))
    except Exception:
        return default_ms


# ---------- Unified delay config ----------
def _get_item_delay_ms() -> int:
    return max(0, _to_ms_env("ITEM_DELAY_MS", 300))


def _sleep_ms(ms: int, *, reason: str) -> None:
    secs = max(0.0, float(ms) / 1000.0)
    _dbg(f"{reason} {ms}ms")
    time.sleep(secs)


def _sleep_default(*, reason: str) -> None:
    _sleep_ms(_get_item_delay_ms(), reason=reason)


ITEM_DELAY_MS = _get_item_delay_ms()

_BROWSER_BINARY_ENV_VARS = (
    "BROWSER_BINARY_PATH",
    "CHROME_BINARY_PATH",
    "CHROMIUM_BINARY_PATH",
)

_LINUX_BROWSER_BINARY_CANDIDATES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)

_BROWSER_BINARY_NAMES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)


def _resolve_existing_executable(value: str) -> Optional[str]:
    value = value.strip()
    if not value:
        return None
    if os.path.exists(value):
        return value
    return shutil.which(value)


def _resolve_browser_binary() -> Optional[str]:
    for env_name in _BROWSER_BINARY_ENV_VARS:
        value = os.getenv(env_name)
        if not value:
            continue
        resolved = _resolve_existing_executable(value)
        if resolved:
            _dbg(f"Using browser binary from {env_name}: {resolved}")
            return resolved
        raise RuntimeError(
            f"{env_name} is set but does not point to an executable browser: {value}"
        )

    for name in _BROWSER_BINARY_NAMES:
        resolved = shutil.which(name)
        if resolved:
            _dbg(f"Using browser binary from PATH: {resolved}")
            return resolved

    for path in _LINUX_BROWSER_BINARY_CANDIDATES:
        if os.path.exists(path):
            _dbg(f"Using browser binary: {path}")
            return path

    return None


def _resolve_chromedriver_path() -> Optional[str]:
    chromedriver_path = os.getenv("CHROMEDRIVER_PATH")
    if chromedriver_path:
        resolved = _resolve_existing_executable(chromedriver_path)
        if resolved:
            return resolved
        raise RuntimeError(
            "CHROMEDRIVER_PATH is set but does not point to an executable driver: "
            f"{chromedriver_path}"
        )
    return shutil.which("chromedriver")


class StepScraper:
    """
    Actions supported:
      load_url, sleep, scroll_to, click_button, select_checkbox, type_text,
      data_extract (extract/redirect/sleep/replace_text/regex_extract/next),
      json_set_payload, json_replace_text, json_data_extract.

    Notes:
      - 'xpath' is a CSS selector (legacy name).
      - Sleep without 'seconds' falls back to ITEM_DELAY_MS.
      - New: run_iter() yields (site, jobs) as each site finishes.
      - New: data_extract supports optional pagination block.
    """

    def __init__(
        self,
        steps_path: str,
        headless: Optional[bool] = None,
        default_wait: float = 10.0,
    ):
        self.steps_path = steps_path
        self.default_wait = default_wait
        env_headless = os.getenv("HEADLESS")
        self.headless = (
            headless
            if headless is not None
            else (env_headless.lower() == "true" if env_headless else False)
        )

    # ---------- Driver ----------
    def _make_driver(self) -> webdriver.Chrome:
        options = webdriver.ChromeOptions()
        browser_binary = _resolve_browser_binary()
        if browser_binary:
            options.binary_location = browser_binary

        if self.headless:
            options.add_argument("--headless=new")
        else:
            options.add_experimental_option("detach", True)

        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1400,900")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        try:
            chromedriver_path = _resolve_chromedriver_path()
            if chromedriver_path:
                _dbg(f"Using ChromeDriver: {chromedriver_path}")
                svc = Service(executable_path=chromedriver_path)
                driver = webdriver.Chrome(service=svc, options=options)
            else:
                _dbg("Using Selenium Manager for ChromeDriver")
                driver = webdriver.Chrome(options=options)
        except WebDriverException as e:
            raise RuntimeError(
                f"Chrome failed to start: {e}\n"
                "For Ubuntu/Chromium, install chromium plus the matching "
                "chromedriver, set HEADLESS=true on a server, and set "
                "CHROMIUM_BINARY_PATH or CHROME_BINARY_PATH plus CHROMEDRIVER_PATH "
                "if Selenium cannot find them automatically."
            ) from e

        driver.implicitly_wait(3)
        return driver

    # ---------- URL utils ----------
    @staticmethod
    def _normalize_url(current_url: str, candidate: Optional[str]) -> Optional[str]:
        if not candidate:
            return None
        cand = str(candidate).strip().replace("\n", "").replace("\r", "")
        if not cand or cand in ("#", "/#"):
            return None
        low = cand.lower()
        if low.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:")):
            return None
        abs_url = urljoin(current_url, cand)
        parsed = urlparse(abs_url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return abs_url
        return None

    # ---------- DOM helpers ----------
    def _safe_find_text(
        self, driver: webdriver.Chrome, css: Optional[str]
    ) -> Optional[str]:
        if not css:
            return None
        try:
            el = driver.find_element(By.CSS_SELECTOR, css)
            return (el.text or "").strip()
        except Exception:
            return None

    def _safe_find_first_outer_html(
        self, driver: webdriver.Chrome, css: str
    ) -> Optional[str]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, css)
            if not els:
                return None
            return els[0].get_attribute("outerHTML")
        except Exception:
            return None

    @staticmethod
    def _parse_page_num(text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        m = re.search(r"(\d+)", text)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    def _click_css(self, driver: webdriver.Chrome, css: str) -> bool:
        try:
            el = WebDriverWait(driver, self.default_wait).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, css))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            try:
                el.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False

    def _is_next_disabled(
        self,
        driver: webdriver.Chrome,
        next_css: str,
        disabled_css: Optional[str],
    ) -> bool:
        # explicit disabled selector (if provided)
        if disabled_css:
            try:
                if driver.find_elements(By.CSS_SELECTOR, disabled_css):
                    return True
            except Exception:
                pass

        # attribute-based detection
        try:
            btn = driver.find_element(By.CSS_SELECTOR, next_css)
            if btn.get_attribute("disabled") is not None:
                return True
            aria_disabled = (btn.get_attribute("aria-disabled") or "").strip().lower()
            if aria_disabled == "true":
                return True
        except Exception:
            # if we cannot find the next button, treat as disabled (stop paging)
            return True

        return False

    @staticmethod
    def _split_extract_steps_for_pagination(
        extract_steps: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        # Paginated scraping is intentionally two-pass:
        # 1. Run the list-page steps on every results page.
        # 2. Run the detail-page steps after all pages have been collected.
        #
        # The redirect step is the boundary between those two phases. This keeps
        # detail-page visits from resetting or confusing the current list page.
        list_steps: List[Dict[str, Any]] = []
        detail_steps: List[Dict[str, Any]] = []
        redirect_step: Optional[Dict[str, Any]] = None
        in_detail = False

        for step in extract_steps:
            action = step.get("action")
            # "next" is the legacy per-item stop marker; steps after it are not
            # part of the extraction recipe for this item.
            if action == "next":
                break
            # First redirect marks the handoff from list data to detail data.
            if action == "redirect" and redirect_step is None:
                redirect_step = step
                in_detail = True
                continue
            if in_detail:
                detail_steps.append(step)
            else:
                list_steps.append(step)

        return list_steps, detail_steps, redirect_step

    def _apply_extract_step(
        self,
        *,
        driver: webdriver.Chrome,
        soup: BeautifulSoup,
        job: Dict[str, Any],
        step: Dict[str, Any],
        redirected: bool,
    ) -> None:
        # This helper is shared by normal and paginated extraction so selector
        # behavior stays consistent between both paths.
        column = step.get("as_column")
        css = step.get("xpath")  # (your schema calls it xpath, but it's CSS)
        attr = step.get("attr_target")
        data_type = (step.get("data_type") or "").lower()

        # Allow "current_url" without a selector
        if not column or (not css and data_type != "current_url"):
            return

        if data_type == "current_url":
            job[column] = driver.current_url
            return

        ctx = (step.get("context") or "list").lower()
        if ctx == "list" and not redirected:
            # List-page values come from the captured item HTML. That avoids
            # querying the live browser DOM after the page has moved elsewhere.
            tag = soup.select_one(css)
            value = (
                tag.get(attr)
                if (attr and tag and tag.has_attr(attr))
                else (tag.get_text(strip=True) if tag else "")
            )
            job[column] = value
            return

        try:
            tag = driver.find_element(By.CSS_SELECTOR, css)
            value = tag.get_attribute(attr) if attr else tag.text
        except NoSuchElementException:
            value = ""
        job[column] = value

    @staticmethod
    def _apply_replace_text(job: Dict[str, Any], step: Dict[str, Any]) -> None:
        col = step.get("using_column")
        tf = step.get("text_find", "")
        tr = step.get("text_replace", "")
        if col in job and isinstance(job[col], str):
            job[col] = job[col].replace(tf, tr)

    @staticmethod
    def _apply_regex_extract(job: Dict[str, Any], step: Dict[str, Any]) -> None:
        src_col = step.get("using_column")
        as_col = step.get("as_column")
        pattern = step.get("regex_pattern")
        if src_col in job and pattern and as_col:
            m = re.search(pattern, str(job[src_col]))
            if m:
                job[as_col] = m.group(1)

    def _run_sleep_step(self, step: Dict[str, Any], *, prefix: str) -> None:
        if "seconds" in step:
            secs = float(step.get("seconds") or 0)
            _dbg(f"{prefix} explicit sleep {secs:.3f}s")
            time.sleep(max(0.0, secs))
        else:
            _sleep_default(reason=f"{prefix} sleep (default)")

    def _hydrate_paginated_job_detail(
        self,
        driver: webdriver.Chrome,
        job: Dict[str, Any],
        detail_steps: List[Dict[str, Any]],
        redirect_step: Optional[Dict[str, Any]],
        base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not redirect_step or not detail_steps:
            return job

        # Resolve relative detail links against the original list page URL.
        # During paginated hydration the driver may already be sitting on the
        # previous job's detail page, so driver.current_url is not reliable.
        using_col = redirect_step.get("using_column")
        wait_css = redirect_step.get("wait_css")
        source_url = base_url or driver.current_url
        detail_url = (
            self._normalize_url(source_url, job.get(using_col))
            if using_col
            else None
        )
        if not detail_url:
            return job

        try:
            # Hydration starts from the URL collected during the list pass.
            _dbg(f"GET detail: {detail_url}")
            driver.get(detail_url)
            redirected = True
            if wait_css:
                WebDriverWait(driver, self.default_wait).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
                )
            _sleep_default(reason="post-redirect sleep (default)")
        except WebDriverException as we:
            print(f"[warn] redirect get() failed: {we}")
            return job

        empty_soup = BeautifulSoup("", "html.parser")
        # Detail steps operate against the live detail page. replace_text and
        # regex_extract can then clean or derive values from fields just read.
        for step in detail_steps:
            action = step.get("action")
            if action == "sleep":
                self._run_sleep_step(step, prefix="Inner")
                continue
            if action == "extract":
                self._apply_extract_step(
                    driver=driver,
                    soup=empty_soup,
                    job=job,
                    step=step,
                    redirected=redirected,
                )
                continue
            if action == "replace_text":
                self._apply_replace_text(job, step)
                continue
            if action == "regex_extract":
                self._apply_regex_extract(job, step)
                continue

        return job

    # ---------- Public (bulk) ----------
    def run(self) -> Dict[str, List[Dict[str, Any]]]:
        """Legacy bulk mode: returns all sites after scraping completes."""
        with open(self.steps_path, "r", encoding="utf-8") as f:
            steps_data = json.load(f)

        driver = self._make_driver()
        site_to_jobs: Dict[str, List[Dict[str, Any]]] = {}

        try:
            for site, steps in steps_data.items():
                jobs, _ = self._run_site(driver, site, steps)
                site_to_jobs[site] = jobs
        finally:
            try:
                driver.quit()
            except Exception:
                pass

        return site_to_jobs

    # ---------- Public (streaming) ----------
    def run_iter(self) -> Iterator[Tuple[str, List[Dict[str, Any]]]]:
        """Streaming mode: yields (site, jobs) as each site completes scraping."""
        with open(self.steps_path, "r", encoding="utf-8") as f:
            steps_data = json.load(f)

        driver = self._make_driver()
        try:
            for site, steps in steps_data.items():
                jobs, err = self._run_site(driver, site, steps)
                yield site, jobs  # allow caller to persist immediately
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    # ---------- Site runner ----------
    def _run_site(
        self, driver: webdriver.Chrome, site: str, steps: List[Dict[str, Any]]
    ) -> tuple[List[Dict[str, Any]], Optional[str]]:
        _dbg(f"=== Site: {site} ===")
        jobs: List[Dict[str, Any]] = []
        json_payload: Optional[str] = None
        err: Optional[str] = None

        try:
            for step in steps:
                action = step.get("action")
                _dbg(f"Step: {action} :: {step}")

                if action == "load_url":
                    driver.get(step["url"])

                if action == "debug_print_dom_by_css":
                    find_css = step.get("find_css")
                    if find_css:
                        dbg_html = self._safe_find_text(driver, find_css)
                        _dbg(f"Debug HTML for selector '{find_css}' = {dbg_html}")

                elif action == "sleep":
                    if "seconds" in step:
                        secs = float(step.get("seconds") or 0)
                        _dbg(f"Top-level explicit sleep {secs:.3f}s")
                        time.sleep(max(0.0, secs))
                    else:
                        _sleep_default(reason="Top-level sleep (default)")

                elif action == "scroll_to":
                    css = step.get("xpath") or step.get("selector")
                    if css:
                        el = WebDriverWait(driver, self.default_wait).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, css))
                        )
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", el
                        )

                elif action == "click_button":
                    css = step.get("xpath") or step.get("selector")
                    if css:
                        el = WebDriverWait(driver, self.default_wait).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, css))
                        )
                        try:
                            el.click()
                        except ElementClickInterceptedException:
                            driver.execute_script("arguments[0].click();", el)

                elif action == "select_checkbox":
                    css = step.get("selector")
                    if css:
                        el = WebDriverWait(driver, self.default_wait).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, css))
                        )
                        if not el.is_selected():
                            try:
                                el.click()
                            except ElementClickInterceptedException:
                                driver.execute_script("arguments[0].click();", el)

                elif action == "type_text":
                    css = step.get("selector")
                    if css:
                        el = WebDriverWait(driver, self.default_wait).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, css))
                        )
                        el.clear()
                        el.send_keys(step.get("text", ""))

                elif action == "data_extract":
                    pagination = step.get("pagination")
                    if isinstance(pagination, dict) and pagination.get("mode"):
                        # Paginated extraction collects every result page before
                        # visiting detail pages, then returns one complete list
                        # for the site's normal persistence/delta logic.
                        jobs.extend(
                            self._extract_from_list_paginated(
                                driver=driver,
                                focus_scope=step.get("focus_scope"),
                                extract_steps=step.get("extract_steps") or [],
                                pagination=pagination,
                            )
                        )
                    else:
                        jobs.extend(
                            self._extract_from_list(
                                driver=driver,
                                focus_scope=step.get("focus_scope"),
                                extract_steps=step.get("extract_steps") or [],
                            )
                        )

                elif action == "json_set_payload":
                    json_payload = driver.execute_script(
                        "return document.body.innerText;"
                    )

                elif action == "json_replace_text":
                    if json_payload is not None:
                        tf = step.get("text_find")
                        tr = step.get("text_replace")
                        if tf == "__strip_js_wrapper__":
                            m = re.search(r"({.*})", json_payload, re.DOTALL)
                            if m:
                                json_payload = m.group(1)
                        else:
                            json_payload = json_payload.replace(tf, tr)

                elif action == "json_data_extract":
                    if json_payload is not None:
                        jobs.extend(self._extract_from_json(json_payload, step))

        except Exception as e:
            err = str(e)

        return jobs, err

    # ---------- Pagination wrapper ----------
    def _extract_from_list_paginated(
        self,
        driver: webdriver.Chrome,
        focus_scope: Optional[str],
        extract_steps: List[Dict[str, Any]],
        pagination: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Pagination config (inside data_extract step):
          {
            "mode": "click_next",
            "max_pages": 10,
            "current_page_css": "nav[aria-label='pagination'] button[aria-current='page']",
            "next_page_css": "nav[aria-label='pagination'] button[aria-label='next']",
            "next_disabled_css": "button[aria-label='next'][disabled], button[aria-label='next'][aria-disabled='true']",
            "page_wait_ms": 1200,
            "page_as_column": "__page"
          }
        """
        if not focus_scope:
            _dbg("No focus_scope provided for pagination.")
            return []

        mode = str(pagination.get("mode") or "").lower().strip()
        _dbg(f"Pagination mode: {mode}")
        if mode != "click_next":
            _dbg("Unknown pagination mode, falling back to single page extraction.")
            return self._extract_from_list(driver, focus_scope, extract_steps)

        max_pages = int(pagination.get("max_pages") or 1)
        max_pages = max(1, min(max_pages, 200))
        _dbg(f"Max pages to paginate: {max_pages}")

        current_page_css = pagination.get("current_page_css")
        next_page_css = (
            pagination.get("next_page_css")
            or "nav[aria-label='pagination'] button[aria-label='next']"
        )
        next_disabled_css = pagination.get("next_disabled_css")
        page_wait_ms = int(pagination.get("page_wait_ms") or 0)
        page_as_column = pagination.get("page_as_column")
        # Split the recipe once before looping pages. list_steps should avoid
        # any redirect/detail work so page traversal stays stable.
        list_steps, detail_steps, redirect_step = (
            self._split_extract_steps_for_pagination(extract_steps)
        )

        all_jobs: List[Dict[str, Any]] = []
        seen_page_sigs: set[str] = set()
        detail_base_url = driver.current_url

        # determine initial page number if possible
        page_num = (
            self._parse_page_num(self._safe_find_text(driver, current_page_css)) or 1
        )
        _dbg(f"Initial page number: {page_num}")

        for page_idx in range(max_pages):
            _dbg(f"Extracting page {page_num} (iteration {page_idx+1}/{max_pages})")
            # Step 1: read only the current page's list cards/rows.
            page_jobs = self._extract_from_list(
                driver=driver,
                focus_scope=focus_scope,
                extract_steps=list_steps,
            )

            if page_as_column and page_jobs:
                for j in page_jobs:
                    # Keep page metadata in raw output JSON for debugging. It is
                    # intentionally not part of Job hashing or the DB schema.
                    j[page_as_column] = page_num

            all_jobs.extend(page_jobs)
            _dbg(f"Extracted {len(page_jobs)} jobs from page {page_num}")

            # Stop immediately on an empty page; there is nothing useful to
            # hydrate and no next-page signal can be trusted.
            if not page_jobs:
                _dbg("No jobs found on this page, stopping pagination.")
                break

            # Step 2: record a compact page signature before clicking next. If a
            # site loops back to the same page, this prevents infinite scraping.
            first_id = str(page_jobs[0].get("JobID") or "").strip()
            page_label = self._safe_find_text(driver, current_page_css) or str(page_num)
            sig = f"{page_label}|{first_id}|{len(page_jobs)}|{driver.current_url}"
            _dbg(f"Page signature: {sig}")
            if sig in seen_page_sigs:
                _dbg("Duplicate page signature detected, stopping pagination.")
                break
            seen_page_sigs.add(sig)

            # stop if no next / disabled next
            if self._is_next_disabled(driver, next_page_css, next_disabled_css):
                _dbg("Next button is disabled or not found, stopping pagination.")
                break

            # Step 3: capture markers that should change after clicking next.
            # Workday may keep the same URL, so we also watch the active page
            # label and first result row.
            before_url = driver.current_url
            before_page = (self._safe_find_text(driver, current_page_css) or "").strip()
            before_first = self._safe_find_first_outer_html(driver, focus_scope) or ""
            _dbg(f"Clicking next: before_url={before_url}, before_page={before_page}")

            if not self._click_css(driver, next_page_css):
                _dbg("Failed to click next button, stopping pagination.")
                break

            # Step 4: wait for evidence that the browser is showing the next
            # result page before extracting again.
            try:
                WebDriverWait(driver, self.default_wait).until(
                    lambda d: (
                        d.current_url != before_url
                        or (
                            (self._safe_find_text(d, current_page_css) or "").strip()
                            != before_page
                            and bool(current_page_css)
                        )
                        or (
                            (self._safe_find_first_outer_html(d, focus_scope) or "")
                            != before_first
                            and bool(before_first)
                        )
                    )
                )
                _dbg("Page advanced after clicking next.")
            except TimeoutException:
                _dbg(
                    "Timeout waiting for page to advance after clicking next. Stopping pagination."
                )
                break

            if page_wait_ms > 0:
                _dbg(f"Sleeping for {page_wait_ms}ms after page advance.")
                _sleep_ms(page_wait_ms, reason="pagination post-click wait")

            # Step 5: prefer the site's visible page label; otherwise keep a
            # local counter so __page remains useful when selectors are missing.
            prev_page_num = page_num
            page_num = self._parse_page_num(
                self._safe_find_text(driver, current_page_css)
            ) or (page_num + 1)
            _dbg(f"Updated page number: {prev_page_num} -> {page_num}")

        if redirect_step and detail_steps and all_jobs:
            # Step 6: now that pagination is complete, hydrate detail-only fields
            # from each collected JobUrl. This is what prevents detail redirects
            # from corrupting the list-page cursor.
            _dbg(f"Hydrating details for {len(all_jobs)} paginated jobs.")
            for job in all_jobs:
                self._hydrate_paginated_job_detail(
                    driver, job, detail_steps, redirect_step, detail_base_url
                )
                if ITEM_DELAY_MS > 0:
                    _sleep_ms(ITEM_DELAY_MS, reason="item delay")

        _dbg(f"Pagination complete. Total jobs extracted: {len(all_jobs)}")
        return all_jobs

    # ---------- List → optional detail ----------
    def _extract_from_list(
        self,
        driver: webdriver.Chrome,
        focus_scope: Optional[str],
        extract_steps: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not focus_scope:
            return []

        wait = WebDriverWait(driver, self.default_wait)
        try:
            wait.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, focus_scope))
            )
        except TimeoutException:
            print(f"[warn] focus_scope not found: {focus_scope}")
            return []

        list_url = driver.current_url
        elements = driver.find_elements(By.CSS_SELECTOR, focus_scope)
        jobs: List[Dict[str, Any]] = []

        for idx, el in enumerate(elements):
            try:
                item_html = el.get_attribute("outerHTML")
            except WebDriverException:
                elements = driver.find_elements(By.CSS_SELECTOR, focus_scope)
                if idx >= len(elements):
                    break
                el = elements[idx]
                item_html = el.get_attribute("outerHTML")

            soup = BeautifulSoup(item_html, "html.parser")
            job: Dict[str, Any] = {}
            redirected = False

            for es in extract_steps:
                es_action = es.get("action")

                if es_action == "sleep":
                    self._run_sleep_step(es, prefix="Inner")
                    continue

                if es_action == "extract":
                    self._apply_extract_step(
                        driver=driver,
                        soup=soup,
                        job=job,
                        step=es,
                        redirected=redirected,
                    )
                    continue

                if es_action == "redirect" and not redirected:
                    using_col = es.get("using_column")
                    link_css = es.get("link_css")
                    wait_css = es.get("wait_css")

                    detail_url = (
                        self._normalize_url(driver.current_url, job.get(using_col))
                        if using_col
                        else None
                    )
                    if not detail_url and link_css:
                        link_tag = soup.select_one(link_css)
                        if link_tag and link_tag.has_attr("href"):
                            detail_url = self._normalize_url(
                                driver.current_url, link_tag["href"]
                            )

                    if detail_url:
                        try:
                            _dbg(f"GET detail: {detail_url}")
                            driver.get(detail_url)
                            redirected = True
                            if wait_css:
                                WebDriverWait(driver, self.default_wait).until(
                                    EC.presence_of_element_located(
                                        (By.CSS_SELECTOR, wait_css)
                                    )
                                )
                        except WebDriverException as we:
                            print(f"[warn] redirect get() failed: {we}")
                            redirected = False

                    if not redirected:
                        try:
                            clickable = (
                                el.find_element(By.CSS_SELECTOR, link_css)
                                if link_css
                                else el.find_element(By.CSS_SELECTOR, "a")
                            )
                            driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});",
                                clickable,
                            )
                            try:
                                clickable.click()
                            except ElementClickInterceptedException:
                                driver.execute_script(
                                    "arguments[0].click();", clickable
                                )
                            redirected = True
                            if wait_css:
                                WebDriverWait(driver, self.default_wait).until(
                                    EC.presence_of_element_located(
                                        (By.CSS_SELECTOR, wait_css)
                                    )
                                )
                        except Exception as ce:
                            print(f"[warn] redirect click failed: {ce}")
                            redirected = False

                    if redirected:
                        _sleep_default(reason="post-redirect sleep (default)")
                    continue

                if es_action == "replace_text":
                    self._apply_replace_text(job, es)
                    continue

                if es_action == "regex_extract":
                    self._apply_regex_extract(job, es)
                    continue

                if es_action == "next":
                    break

            if redirected:
                # IMPORTANT: prefer back() to preserve pagination/session state
                try:
                    driver.back()
                    WebDriverWait(driver, self.default_wait).until(
                        EC.presence_of_all_elements_located(
                            (By.CSS_SELECTOR, focus_scope)
                        )
                    )
                except Exception:
                    try:
                        driver.get(list_url)
                        WebDriverWait(driver, self.default_wait).until(
                            EC.presence_of_all_elements_located(
                                (By.CSS_SELECTOR, focus_scope)
                            )
                        )
                    except Exception:
                        pass

            jobs.append(job)

            if ITEM_DELAY_MS > 0:
                _sleep_ms(ITEM_DELAY_MS, reason="item delay")

        return jobs

    # ---------- JSON ----------
    def _extract_from_json(
        self, json_payload: str, step: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        extract_steps = step.get("extract_steps") or []
        focus_scope = step.get("focus_scope")
        data = json.loads(json_payload)
        items = data.get(focus_scope, [])
        out: List[Dict[str, Any]] = []
        for it in items:
            job: Dict[str, Any] = {}
            for es in extract_steps:
                if es.get("action") != "extract":
                    continue
                column = es.get("as_column")
                key = es.get("key")
                if not column:
                    continue
                cur: Any = it
                if key:
                    for part in re.split(r"[>.]", key):
                        cur = cur.get(part) if isinstance(cur, dict) else None
                        if cur is None:
                            break
                job[column] = cur if cur is not None else ""
            out.append(job)
        return out
