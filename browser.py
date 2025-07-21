from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, NoSuchElementException, TimeoutException
import json
import time
from urllib.parse import urljoin, urlparse

class Browser:
    def __init__(self, driver, default_timeout=10):
        self.driver = driver
        self.default_timeout = default_timeout

    def load_url(self, url):
        print("Redirect call.." + str(url))
        try:
            # If absolute, use as-is. If relative, join to current browser location's origin.
            if url.lower().startswith(('http://', 'https://')):
                full_url = url
            else:
                # Get current browser URL (should be absolute) and extract its origin
                current_url = self.driver.current_url
                parsed = urlparse(current_url)
                origin = f"{parsed.scheme}://{parsed.netloc}/"
                full_url = urljoin(origin, url)

            print(f"Navigating to: {full_url}")
            self.driver.get(full_url)
            time.sleep(2)
            return True
        except Exception as e:
            print(f"Error loading URL {url}: {e}")
            return False

    def click_button(self, selector, timeout=None):
        try:
            wait = WebDriverWait(self.driver, timeout or self.default_timeout)
            button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
            button.click()
            return True
        except Exception as e:
            print(f"Error clicking button ({selector}): {e}")
            return False

    def scroll_to(self, xpath, timeout=None):
        try:
            wait = WebDriverWait(self.driver, timeout or self.default_timeout)
            element = wait.until(EC.presence_of_element_located((By.XPATH, xpath)))
            self.driver.execute_script("arguments[0].scrollIntoView();", element)
            return True
        except Exception as e:
            print(f"Error scrolling to element ({xpath}): {e}")
            return False

    def click_button_xpath(self, selector, timeout=None):
        try:
            wait = WebDriverWait(self.driver, timeout or self.default_timeout)
            button = wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
            button.click()
            return True
        except Exception as e:
            print(f"Error clicking button by XPath ({selector}): {e}")
            return False

    def select_checkbox(self, selector, timeout=None):
        try:
            wait = WebDriverWait(self.driver, timeout or self.default_timeout)
            checkbox = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
            checkbox.click()
            return True
        except Exception as e:
            print(f"Error selecting checkbox ({selector}): {e}")
            return False

    def type_text(self, selector, text, timeout=None):
        try:
            wait = WebDriverWait(self.driver, timeout or self.default_timeout)
            text_box = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, selector)))
            text_box.clear()
            text_box.send_keys(text)
            return True
        except Exception as e:
            print(f"Error typing text into ({selector}): {e}")
            return False

    def get_driver(self):
        return self.driver

    @staticmethod
    def save_to_json(data, filename):
        with open(filename, "w") as json_file:
            json.dump(data, json_file, indent=4)

    def find_element_safely(self, by, locator, retries=3):
        attempt = 0
        while attempt < retries:
            try:
                return self.driver.find_element(by, locator)
            except StaleElementReferenceException:
                attempt += 1
                time.sleep(0.5)
            except Exception as e:
                print(f"Error finding element ({locator}): {e}")
                return None
        print(f"Failed to find element after {retries} attempts: {locator}")
        return None

    def quit(self):
        self.driver.quit()
