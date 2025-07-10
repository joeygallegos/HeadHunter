import json
import time
from selenium import webdriver
from selenium import webdriver
from selenium.webdriver.common.by import By
import re
import nltk
from collections import Counter

import gspread
from google.oauth2.service_account import Credentials

import pprint
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# Custom object
from browser import Browser

# Load config
with open("config.json", "r") as config_file:
    config = json.load(config_file)

# Global variables
global test
test = False  # Flag to indicate if we are running in test mode

# Initialize Google Sheets API
def init_google_sheet():
    creds = Credentials.from_service_account_file(config['GOOGLE_CREDENTIALS_PATH'], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)
    return client.open_by_key(config['SHEET_ID']).worksheet(config['SHEET_NAME'])

# Take the job description and pluck relevant keywords
def extract_keywords(job_description, top_n=10):
    """
    Extracts the top N relevant keywords from a job description.
    Returns a list of keywords.
    """
    # Clean text: remove special characters
    cleaned = re.sub(r'[^a-zA-Z\s]', '', job_description)
    
    # Tokenize
    tokens = nltk.word_tokenize(cleaned.lower())
    
    # Part-of-speech tagging
    pos_tags = nltk.pos_tag(tokens)
    
    # Keep nouns and adjectives only
    candidate_words = [
        word for word, pos in pos_tags
        if pos.startswith('NN') or pos.startswith('JJ')
    ]
    
    # Count frequencies
    word_counts = Counter(candidate_words)
    
    # Get the top N keywords
    keywords = [word for word, count in word_counts.most_common(top_n)]
    
    return keywords

# Initialize worksheet globally
worksheet = init_google_sheet()

def append_to_google_sheet(data):
    """
    Appends a row of data to the global Google Sheet worksheet.
    Ensures all values are strings.
    :param data: List of values to be appended as a row
    """
    str_data = [str(item) if item is not None else "" for item in data]
    try:
        worksheet.append_row(str_data, value_input_option="RAW")
        print("Data successfully appended to Google Sheet.")
    except Exception as e:
        print(f"Error: {e}")

def job_id_exists(job_id):
    """Check if the JobID already exists in the sheet."""
    existing_ids = worksheet.col_values(1)  # Assuming JobID is in column A (index 1)
    return job_id in existing_ids

def do_steps(test=False):
    """
    Executes a series of automated browser steps for job scraping as defined in a steps.json file.

    This function initializes a Chrome WebDriver, loads step instructions for multiple job sites,
    and performs actions such as loading URLs, clicking buttons, scrolling, typing text, extracting data,
    and handling redirections. For each job posting found, it extracts relevant information, checks for duplicates,
    and appends new jobs to a Google Sheet and a JSON file.

    Steps supported include:
        - load_url: Navigate to a specified URL.
        - sleep: Pause execution for a specified duration.
        - scroll_to: Scroll to an element identified by XPath.
        - click_button: Click a button using a selector or XPath.
        - select_checkbox: Select a checkbox using a selector.
        - type_text: Enter text into an input field.
        - data_extract: Extract job data using a sequence of extraction steps, optionally following links to detail pages.
        - replace_text: Replace text in extracted data.
        - next: Move to the next job posting.

    Extracted job data is saved per site in a JSON file and new jobs are appended to a Google Sheet.

    Dependencies:
        - Selenium WebDriver
        - BeautifulSoup
        - steps.json configuration file
        - Google Sheets integration

    Raises:
        - Various exceptions may be raised by Selenium or file operations.
    """

    # Set up Chrome WebDriver
    options = webdriver.ChromeOptions()
    driver = webdriver.Chrome(options=options)
    driver.implicitly_wait(3)

    browser = Browser(driver)

    if test:
        with open("test.json", "r") as steps_file:
            steps_data = json.load(steps_file)
    else:
        # Load steps from steps.json
        with open("steps.json", "r") as steps_file:
            steps_data = json.load(steps_file)

    # Execute steps for each site
    for site, steps in steps_data.items():
        jobs = []
        json_payload = None # Initialize an empty JSON payload for the json processing steps

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
                
                print("Array of jobs DOM length:", len(array_of_jobs_dom))
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

                    job_id = job_data.get("JobID")
                    if job_id and not job_id_exists(job_id):
                        row = [job_data.get("JobID", ""), job_data.get("JobTitle", ""), job_data.get("JobUrl", ""), ", ".join(extract_keywords(job_data.get("JobDesc", ""))), job_data.get("JobDesc", "")]
                        if not test:
                            append_to_google_sheet(row)
                        print(f"Added job: {job_id}")
                    else:
                        print(f"Skipped job (already exists): {job_id}")
            elif action == "json_set_payload":
                json_payload = browser.get_driver().execute_script("return document.body.innerText;") # Set the JSON payload to process
            elif action == "json_replace_text":
                if json_payload is not None:
                    text_find = step.get("text_find")
                    text_with = step.get("text_replace")

                    if text_find == "__strip_js_wrapper__":
                        # Remove any text before the first { and after the last }
                        match = re.search(r'({.*})', json_payload, re.DOTALL)
                        if match:
                            json_payload = match.group(1)
                    else:
                        json_payload = json_payload.replace(text_find, text_with)
            elif action == "json_data_extract":
                if json_payload is None:
                    print("No JSON payload set. Skipping data extraction step.")
                    continue

                extract_steps = step.get("extract_steps") # The steps to extract data from the JSON response
                focus_scope = step.get("focus_scope") # The array to focus on

                # Cast and Parse the JSON response
                json_data = json.loads(json_payload)

                # Extract the array of jobs from the JSON data
                array_of_jobs = json_data.get(focus_scope, [])
                if not array_of_jobs:
                    print(f"No jobs found in the focus array: {focus_scope}")
                    continue
                # CONTINUE HERE
                print("Array of jobs length:", len(array_of_jobs))
                for job_item in array_of_jobs:
                    job_data = {}

                    print("==============")
                    print("New job, setting back to false")
                    redirected_to_details_page = False

                    for extract_step in extract_steps:
                        extract_step_action = extract_step.get("action")

                        if extract_step_action == "extract":
                            column = extract_step.get("as_column")
                            json_key = extract_step.get("key")

                            # Support nested keys like "job>title" or "job.title"
                            value = ""
                            if json_key:
                                keys = re.split(r'[>.]', json_key)
                                current = job_item
                                for key in keys:
                                    if isinstance(current, dict):
                                        current = current.get(key)
                                    else:
                                        current = None
                                        break
                                if current is not None:
                                    value = current
                                else:
                                    value = ""

                            job_data[column] = value
                            print("keypair:", column, value)

                        elif extract_step_action == "next":
                            # You might handle pagination or move to next item; depends on your design
                            pass
                    
                    jobs.append(job_data)

                    # Attempt to add the job to the Google Sheet and JSON file
                    attempt_add_job(job_data)
                    
        browser.save_to_json(jobs, f"{site}_job_postings.json")

def attempt_add_job(job_data=None):
    """
    Adds a new job to the Google Sheet and JSON file.
    This function is a placeholder for future implementation.
    """
    if job_data is None:
        print("No job data provided to attempt_add_job.")
        return
    # Check if the job already exists in the Google Sheet
    job_id = job_data.get("JobID")
    if job_id and not job_id_exists(job_id):
        row = [job_data.get("JobID", ""), job_data.get("JobTitle", ""), job_data.get("JobUrl", ""), ", ".join(extract_keywords(job_data.get("JobDesc", ""))), job_data.get("JobDesc", "")]
        
        # If not in test mode, append to Google Sheet
        if not test:
            append_to_google_sheet(row)
            print(f"Added job: {job_id}")
        else:
            print(f"Skipped job (already exists): {job_id}")


if __name__ == "__main__":
    command = input("Enter command (download/steps): ").lower()
    if command == "steps":
        do_steps()
    elif command == "test":
        do_steps(True)
    elif command == "download":
        nltk.download('punkt')
        nltk.download('averaged_perceptron_tagger_eng')
        nltk.download('punkt_tab')
    elif command == "words":
        print("Extracting keywords from job descriptions...")
        desc = """
        We are looking for a motivated Python developer to join our fast-growing team.
        The ideal candidate should have experience in Django, REST APIs, and cloud technologies.
        Strong problem-solving and communication skills are essential.
        """
        keywords = extract_keywords(desc)
        print(keywords)
    else:
        print("Invalid command.")
