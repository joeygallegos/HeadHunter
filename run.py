import json
import time
from selenium import webdriver
from selenium import webdriver
from selenium.webdriver.common.by import By

import pprint
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# Custom object
from browser import Browser


def do_steps():
    # Set up Chrome WebDriver
    options = webdriver.ChromeOptions()
    driver = webdriver.Chrome(options=options)
    driver.implicitly_wait(3)

    browser = Browser(driver)

    # Load steps from steps.json
    with open("steps.json", "r") as steps_file:
        steps_data = json.load(steps_file)

    # Execute steps for each site
    for site, steps in steps_data.items():
        jobs = []
        # Execute steps for the given site
        for step in steps:
            action = step["action"]
            if action == "load_url":
                browser.load_url(step["url"])
            elif action == "sleep":
                time.sleep(15)
            elif action == "scroll_to":
                browser.scroll_to(step["xpath"])
            elif action == "click_button":
                if step.get("xpath") is not None:
                    browser.click_button_xpath(step["xpath"])
                else:
                    browser.click_button(step["selector"])
            elif action == "select_checkbox":
                browser.select_checkbox(step["selector"])
            elif action == "type_text":
                browser.type_text(step["selector"], step["text"])
            elif action == "data_extract":
                extract_steps = step.get("extract_steps")
                focus_scope = step.get("focus_scope")
                array_of_jobs_dom = []
                array_of_driver_obj = browser.get_driver().find_elements(
                    By.CSS_SELECTOR, focus_scope
                )

                print("Saving driver list items HTML as array list")
                for driver_obj_ref in array_of_driver_obj:
                    array_of_jobs_dom.append(driver_obj_ref.get_attribute("outerHTML"))

                for job_html in array_of_jobs_dom:
                    job_data = {}
                    initial_url = browser.get_driver().current_url

                    print("==============")
                    print("New job, setting back to false")
                    redirected_to_details_page = False

                    for extract_step in extract_steps:
                        extract_step_action = extract_step.get("action")
                        extract_type = extract_step.get("data_type")
                        column = extract_step.get("as_column")
                        xpath = extract_step.get("xpath")
                        target = extract_step.get("attr_target")
                        web_object = None

                        if extract_step_action == "extract":
                            usingBeautifulSoup = False
                            if xpath is not None:
                                if redirected_to_details_page:
                                    web_object = browser.get_driver().find_element(
                                        By.CSS_SELECTOR, xpath
                                    )
                                else:
                                    usingBeautifulSoup = True
                                    soup = BeautifulSoup(job_html, "html.parser")

                                value = ""

                                if not usingBeautifulSoup:
                                    if target is not None:
                                        value = web_object.get_attribute(target)
                                    else:
                                        value = web_object.text
                                else:
                                    tag = soup.select_one(xpath)
                                    # TODO: If tag is None, fail hard??
                                    if target is not None and tag.has_attr(target):
                                        value = soup.select_one(xpath).get(target)

                                        # TODO: If URL scheme not absolute, then make it
                                        if extract_type == "url":
                                            parsed_url = urlparse(initial_url)
                                            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                                            value = base_url + value
                                    else:
                                        value = soup.select_one(xpath).text.strip()

                                if column in job_data:
                                    if value not in job_data[column]:
                                        job_data[column].append(value)
                                else:
                                    job_data[column] = value

                                if column in ["JobTitle", "JobID", "JobUrl"]:
                                    print("keypair:", column, value)

                                if redirected_to_details_page:
                                    browser.load_url(initial_url)
                                    redirected_to_details_page = False  # Ensure the flag is reset after returning

                        elif extract_step_action == "redirect":
                            redirected_to_details_page = True
                            url_from_memory = job_data.get(
                                extract_step.get("using_column")
                            )

                            if url_from_memory:
                                browser.load_url(url_from_memory)
                            else:
                                print(
                                    f"URL not found for column {extract_step.get('using_column')}"
                                )
                                redirected_to_details_page = (
                                    False  # Prevent indefinite loop if URL is not found
                                )
                        elif extract_step_action == "replace_text":
                            impacted_column = extract_step.get("using_column")
                            text_find = extract_step.get("text_find")
                            text_with = extract_step.get("text_replace")
                            if impacted_column in job_data:
                                job_data[impacted_column] = str(
                                    job_data[impacted_column]
                                ).replace(text_find, text_with)
                        elif extract_step_action == "next":
                            print("NEXT JOB")
                            break  # Exit the inner loop to move on to the next job_html

                    jobs.append(job_data)

        browser.save_to_json(jobs, f"{site}_job_postings.json")


if __name__ == "__main__":
    command = input("Enter command (scrape/steps): ").lower()
    if command == "steps":
        do_steps()
    else:
        print("Invalid command.")
