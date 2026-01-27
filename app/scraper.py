# /app/scraper.py
from __future__ import annotations

import json
import os
import re
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

        chromedriver_path = os.getenv("CHROMEDRIVER_PATH")
        try:
            if chromedriver_path and os.path.exists(chromedriver_path):
                _dbg(f"Using local ChromeDriver: {chromedriver_path}")
                svc = Service(executable_path=chromedriver_path)
                driver = webdriver.Chrome(service=svc, options=options)
            else:
                _dbg("Using Selenium Manager for ChromeDriver")
                driver = webdriver.Chrome(options=options)
        except WebDriverException as e:
            raise RuntimeError(
                f"Chrome failed to start: {e}\n"
                "Set CHROMEDRIVER_PATH in .env to a driver matching your Chrome."
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
                    if "seconds" in es:
                        secs = float(es.get("seconds") or 0)
                        _dbg(f"Inner explicit sleep {secs:.3f}s")
                        time.sleep(max(0.0, secs))
                    else:
                        _sleep_default(reason="Inner sleep (default)")
                    continue

                if es_action == "extract":
                    ctx = (es.get("context") or "list").lower()
                    column = es.get("as_column")
                    css = es.get("xpath")  # (your schema calls it xpath, but it's CSS)
                    attr = es.get("attr_target")
                    data_type = (es.get("data_type") or "").lower()

                    # Allow "current_url" without a selector
                    if not column or (not css and data_type != "current_url"):
                        continue

                    # Special case: use the browser's current URL
                    if data_type == "current_url":
                        job[column] = driver.current_url
                        continue

                    if ctx == "list" and not redirected:
                        tag = soup.select_one(css)
                        value = (
                            tag.get(attr)
                            if (attr and tag and tag.has_attr(attr))
                            else (tag.get_text(strip=True) if tag else "")
                        )
                        job[column] = value
                    else:
                        try:
                            tag = driver.find_element(By.CSS_SELECTOR, css)
                            value = tag.get_attribute(attr) if attr else tag.text
                        except NoSuchElementException:
                            value = ""
                        job[column] = value
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
                    col = es.get("using_column")
                    tf = es.get("text_find", "")
                    tr = es.get("text_replace", "")
                    if col in job and isinstance(job[col], str):
                        job[col] = job[col].replace(tf, tr)
                    continue

                if es_action == "regex_extract":
                    src_col = es.get("using_column")
                    as_col = es.get("as_column")
                    pattern = es.get("regex_pattern")
                    if src_col in job and pattern and as_col:
                        m = re.search(pattern, str(job[src_col]))
                        if m:
                            job[as_col] = m.group(1)
                    continue

                if es_action == "next":
                    break

            if redirected:
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
