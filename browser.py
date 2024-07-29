from selenium import webdriver
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException
import json
import time


class Browser:
    def __init__(self, driver):
        self.driver = driver

    def load_url(self, url):
        print("Redirect call..")
        self.driver.get(url)
        time.sleep(2)

    def click_button(self, selector):
        button = self.driver.find_element(By.CSS_SELECTOR, selector)
        button.click()
        time.sleep(2)

    def scroll_to(self, xpath):
        element = self.driver.find_element(By.XPATH, xpath)
        self.driver.execute_script("arguments[0].scrollIntoView();", element)

    def click_button_xpath(self, selector):
        button = self.driver.find_element(By.XPATH, selector)
        button.click()
        time.sleep(2)

    def select_checkbox(self, selector):
        checkbox = self.driver.find_element(By.CSS_SELECTOR, selector)
        checkbox.click()
        time.sleep(2)

    def type_text(self, selector, text):
        text_box = self.driver.find_element(By.CSS_SELECTOR, selector)
        text_box.clear()  # Clear any existing text
        text_box.send_keys(text)
        time.sleep(2)

    def get_driver(self):
        return self.driver

    @staticmethod
    def save_to_json(data, filename):
        with open(filename, "w") as json_file:
            json.dump(data, json_file, indent=4)

    def find_element_safely(self, by, locator):
        try:
            return self.driver.find_element(by, locator)
        except StaleElementReferenceException:
            return self.find_element_safely(by, locator)

    def quit(self):
        self.driver.quit()
