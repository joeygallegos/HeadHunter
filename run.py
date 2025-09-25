import json
import time
from selenium import webdriver
from selenium import webdriver
from selenium.webdriver.common.by import By
import re
import nltk
from collections import Counter
import os
import gspread
from google.oauth2.service_account import Credentials
import sys
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from urllib.parse import urljoin
import getpass

# Custom object
from browser import Browser
from selenium.common.exceptions import NoSuchElementException

# Load config from the directory of this file
script_dir = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(script_dir, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

print(f"[diag] user={getpass.getuser()} cwd={os.getcwd()} script_dir={script_dir}")

config_path = os.path.join(script_dir, "config.json")
if not os.path.exists(config_path):
    raise FileNotFoundError("config.json file not found. Please ensure it exists in the script directory.")

with open(config_path, "r") as config_file:
    config = json.load(config_file)

# Global variable to indicate if we are running in test mode
test = False  # Flag to indicate if we are running in test mode

# Initialize Google Sheets API
def init_google_sheet():
    creds_path = os.path.join(script_dir, config['GOOGLE_CREDENTIALS_PATH'])
    creds = Credentials.from_service_account_file(creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)
    return client.open_by_key(config['SHEET_ID']).worksheet(config['SHEET_NAME'])

def save_site_json(site: str, jobs: list, output_dir: str = OUTPUT_DIR) -> str:
    """
    Write jobs to <output_dir>/<site>_job_postings.json using an absolute path.
    Atomic-ish replace to avoid partial files if the task gets interrupted.
    """
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, f"{site}_job_postings.json")
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, out_path)  # works across Windows/Unix on same volume
    print(f"[save] wrote {out_path}")
    return out_path

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

def do_steps():
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
        steps_path = os.path.join(script_dir, "test.json")
    else:
        steps_path = os.path.join(script_dir, "steps.json")
    with open(steps_path, "r") as steps_file:
        steps_data = json.load(steps_file)

    # Execute steps for each site
    for site, steps in steps_data.items():
        jobs = []
        json_payload = None # Initialize an empty JSON payload for the json processing steps

        try:
            # Execute steps for a given site
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
                                        try:
                                            web_object = browser.get_driver().find_element(
                                                By.CSS_SELECTOR, xpath
                                            )
                                        except NoSuchElementException:
                                            print(f"NoSuchElementException: Could not find element with xpath '{xpath}' on details page. Skipping job.")
                                            break  # Skip this job and move to next job_html
                                    else:
                                        usingBeautifulSoup = True
                                        print("Using BeautifulSoup to parse job HTML")
                                        soup = BeautifulSoup(job_html, "html.parser")

                                    value = ""

                                    if not usingBeautifulSoup:
                                        if target is not None:
                                            print(f"Extracting attribute '{target}' from element with xpath '{xpath}'")
                                            value = web_object.get_attribute(target)
                                        else:
                                            print(f"Extracting text from element with xpath '{xpath}'")
                                            value = web_object.text
                                    else:
                                        tag = soup.select_one(xpath)
                                        if tag is None:
                                            print(f"Could not find element with selector '{xpath}' in job HTML. Skipping job.")
                                            break  # Skip this job and move to next job_html
                                        if target is not None and tag.has_attr(target):
                                            value = soup.select_one(xpath).get(target)

                                            # FIX: Robust URL handling
                                            if extract_type == "url":
                                                value = urljoin(initial_url, value)
                                        else:
                                            value = tag.text.strip()

                                    if column in job_data:
                                        if value not in job_data[column]:
                                            job_data[column].append(value)
                                    else:
                                        job_data[column] = value

                                    if column in ["JobTitle", "JobID", "JobUrl"]:
                                        print("pair:", column, value)

                                    if redirected_to_details_page:
                                        browser.load_url(initial_url)
                                        redirected_to_details_page = False  # Ensure the flag is reset after returning


                            elif extract_step_action == "redirect":
                                redirected_to_details_page = True
                                
                                # Get the URL from the step data saved to memory
                                url_from_step_memory = job_data.get(
                                    extract_step.get("using_column")
                                )
    
                                # TODO: Handle bug where URL is absolute or just next path
                                if url_from_step_memory:
                                    print(
                                        f"Loading URL from step memory.."
                                    )
                                    browser.load_url(url_from_step_memory)
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
                            elif extract_step_action == "regex_extract":
                                impacted_column = extract_step.get("using_column")
                                as_column = extract_step.get("as_column")
                                regex_pattern = extract_step.get("regex_pattern")
                                if impacted_column in job_data:
                                    match = re.search(regex_pattern, job_data[impacted_column])
                                    if match:
                                        job_data[as_column] = match.group(1)
                                    else:
                                        print(f"No match found for regex '{regex_pattern}' in column '{impacted_column}'")
                            elif extract_step_action == "next":
                                print("NEXT JOB")
                                break  # Exit the inner loop to move on to the next job_html

                        jobs.append(job_data)
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

            # Find canned text across all job postings for this site and remove it
            print("Removing canned text from job descriptions...")
            jobs = remove_canned_text(jobs)

            # Save the jobs to a JSON file for the site            
            save_site_json(site, jobs)
            
            # Attempt to add the jobs to the Google Sheet and JSON file
            attempt_add_jobs(jobs)

        except NoSuchElementException as e:
            print(f"NoSuchElementException encountered on site '{site}': {e}. Skipping to next site.")
            continue
        except Exception as e:
            print(f"Exception encountered on site '{site}': {e}. Skipping to next site.")
            continue

def remove_canned_text(jobs):
    """
    Removes sentences that appear in every job's JobDesc.
    Prints out the removed sentences and returns the cleaned jobs list.
    """
    # Need at least two descriptions to compare
    descs = [job.get("JobDesc", "") for job in jobs if job.get("JobDesc")]
    if len(descs) < 2:
        return jobs

    # Ensure punkt is available
    try:
        nltk.data.find('tokenizers/punkt')
    except LookupError:
        nltk.download('punkt')

    # Split each description into sentences
    desc_sentences = [nltk.sent_tokenize(desc) for desc in descs]

    # Flatten and count
    all_sents = [s.strip() for desc in desc_sentences for s in desc]
    counts  = Counter(all_sents)
    num_descs = len(desc_sentences)

    # A recurring sentence appears in every description at least once
    recurring = {s for s, c in counts.items() if c >= num_descs}
    # Double-check it really shows up in each list
    recurring = {s for s in recurring if all(s in desc for desc in desc_sentences)}

    if recurring:
        print("Recurring sentences removed from all job descriptions:")
        for s in recurring:
            print(f"- {s}")

        # Remove them from each JobDesc
        for job in jobs:
            desc = job.get("JobDesc", "")
            if not desc:
                continue
            sents = nltk.sent_tokenize(desc)
            filtered = [s for s in sents if s.strip() not in recurring]
            job["JobDesc"] = " ".join(filtered).strip()

    return jobs


def attempt_add_jobs(jobs=None):
    """
    Adds new jobs to the Google Sheet and JSON file.
    Processes an array of job dictionaries, checking for duplicates and extracting relevant fields.
    Only adds discovery date for new jobs.
    """
    if not jobs or not isinstance(jobs, list):
        print("No jobs provided to attempt_add_jobs.")
        return

    for job_data in jobs:
        if job_data is None:
            continue
        job_id = job_data.get("JobID")
        if job_id and not job_id_exists(job_id):
            discovery_date = time.strftime("%m/%d/%Y")
            job_desc = job_data.get("JobDesc", "")
            job_level = get_job_level(job_data)
            pay_list = scan_for_pay_range(job_desc)
            job_pay = pay_list[0].strip() if pay_list else "Unknown"
            job_keywords = ", ".join(extract_keywords(job_desc))

            # Prepare the row to append
            row = [
                job_data.get("JobID", ""),
                job_data.get("JobTitle", ""),
                job_data.get("JobUrl", ""),
                job_keywords,
                job_desc,
                job_level,
                job_pay,
                "",  # Placeholder for Swipe
                discovery_date  # Only set for new jobs
            ]

            # If not in test mode, append to Google Sheet
            if not test:
                append_to_google_sheet(row)
                print(f"Added job: {job_id}")
            else:
                print(f"Test mode: would add job {job_id}")
        else:
            print(f"Skipped job (already exists or missing ID): {job_id}")

def get_job_level(job_data=None):
    """
    Returns the job level based on the current test mode.
    """
    if job_data is None:
        print("No job data provided to get_job_level.")
        return "Unknown"
    
    job_title = job_data.get("JobTitle", "")
    job_title_lower = job_title.lower()

    leader_keywords = ["vp", "director", "manager", "head", "chief"]
    senior_keywords = ["senior", "sr.", "lead", "iii"]
    junior_keywords = ["junior", "jr.", "entry-level", "associate", "trainee"]
    architect_keywords = ["architect", "principal"]

    if any(keyword in job_title_lower for keyword in leader_keywords):
        return "Leader"
    elif any(keyword in job_title_lower for keyword in senior_keywords):
        return "Senior"
    elif any(keyword in job_title_lower for keyword in junior_keywords):
        return "Junior"
    elif any(keyword in job_title_lower for keyword in architect_keywords):
        return "Architect"
    else:
        return "Unknown"

def scan_for_pay_range(text):
    """
    Extracts pay ranges or single pay numbers from text.
    Returns a list of unique, clean string(s).
    """
    currency = r"(\$|€|£|USD|EUR|GBP)?"
    num = r"([\d,]+(?:\.\d+)?)"

    # Tight range: e.g. "$109,700 - $203,600" or "184,000 USD - 356,500 USD" or "$25 to $35"
    range_patterns = [
        rf"{currency}\s*{num}\s*(?:-|–|to)\s*{currency}\s*{num}"
    ]

    # Single values (salary or hourly)
    single_patterns = [
        rf"{currency}\s*{num}\s*(?:per year|per annum|year|annum|annual)\b",
        rf"{currency}\s*{num}\s*(?:per hour|hour|hr|hourly)\b",
        rf"{currency}\s*{num}\b"
    ]

    # Ranges first
    found_ranges = []
    for pattern in range_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            raw = m.group(0).strip()
            found_ranges.append(raw)

    if found_ranges:
        # Pick the range with the highest "high" number, or the longest string
        def get_high(raw):
            nums = re.findall(r"[\d,]+(?:\.\d+)?", raw)
            if len(nums) == 2:
                return float(nums[1].replace(",", ""))
            return 0
        found_ranges.sort(key=lambda x: (get_high(x), len(x)), reverse=True)
        return [found_ranges[0]]

    # Singles
    found_singles = []
    for pattern in single_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            raw = m.group(0).strip()
            if raw and raw not in found_singles:
                found_singles.append(raw)

    return found_singles

if __name__ == "__main__":

    if len(sys.argv) < 2:
        print("Usage: python run.py [download|steps|test|words]")
        sys.exit(1)

    command = sys.argv[1].lower()
    if command == "steps":
        do_steps()
    elif command == "test":
        test = True
        do_steps()
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
