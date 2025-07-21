import json
import os
from flask import Flask, render_template_string, request, jsonify
import gspread
from google.oauth2.service_account import Credentials
import re
import html

app = Flask(__name__)

# Load config from the directory of this file
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")
if not os.path.exists(config_path):
    raise FileNotFoundError("config.json file not found. Please ensure it exists in the script directory.")

with open(config_path, "r") as config_file:
    config = json.load(config_file)

# Global variables
global test
test = False  # Flag to indicate if we are running in test mode

# Initialize Google Sheets API
def init_google_sheet():
    creds_path = os.path.join(script_dir, config['GOOGLE_CREDENTIALS_PATH'])
    creds = Credentials.from_service_account_file(creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)
    return client.open_by_key(config['SHEET_ID']).worksheet(config['SHEET_NAME'])

def get_jobs():
    sheet = init_google_sheet()
    jobs = sheet.get_all_records()
    filtered_jobs = [job for job in jobs if not job.get('Swipe')]
    # Highlight the important sentence for each job
    for job in filtered_jobs:
        # Use safe default for missing JobTitle
        job['JobDescHighlighted'] = highlight_as_you_will(job.get('JobTitle', ''), job.get('JobDesc', ''))
    return filtered_jobs

def highlight_as_you_will(job_title, job_desc):
    if not job_title or not job_desc:
        return job_desc or ""
    # Regex: Find "As a [anything] you will..." until first period
    pattern = rf"(As a\s+.*?you will.*?\.)"
    def replacer(match):
        return f"<mark class='bg-yellow-200'>{html.escape(match.group(1))}</mark>"
    # Only highlight the first match
    highlighted = re.sub(pattern, replacer, job_desc, count=1, flags=re.IGNORECASE | re.DOTALL)
    return highlighted


@app.route('/')
def index():
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Job Tinder</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
    </head>
    <body class="bg-gray-100 min-h-screen flex items-center justify-center">
        <div id="card" class="w-full max-w-4xl mx-auto bg-white p-8 rounded-xl shadow-lg" x-data="jobSwiper()" x-init="fetchJobs()">
            <template x-if="jobs.length > 0 && idx < jobs.length">
                <div>
                    <h2 class="text-2xl font-bold mb-2" x-text="currentJob().JobTitle || 'No Title'"></h2>
                    <p class="mb-1">
                        <span class="font-semibold">Level:</span>
                        <span 
                            x-text="currentJob().JobLevel || ''"
                            :class="{
                                'bg-red-500 text-white px-2 rounded': (currentJob().JobLevel || '').toLowerCase() === 'architect',
                                'bg-orange-400 text-white px-2 rounded': (currentJob().JobLevel || '').toLowerCase() === 'senior'
                            }"
                        ></span>
                    </p>
                    <p class="mb-4"><span class="font-semibold">Pay:</span> <span x-text="currentJob().JobPay || ''"></span></p>
                    <p class="mb-2" x-html="currentJob().JobDescHighlighted || 'No Description'"></p>
                    <p class="mb-1"><span class="font-semibold">Keywords:</span> <span x-text="currentJob().Keywords || ''"></span></p>
                    <template x-if="currentJob().JobUrl">
                        <p class="mb-4">
                            <a :href="currentJob().JobUrl" class="text-blue-600 underline" target="_blank">Job Link</a>
                        </p>
                    </template>
                    <div class="flex justify-center space-x-8 mt-6">
                        <button class="bg-red-500 hover:bg-red-600 text-white px-6 py-2 rounded-lg text-lg font-semibold"
                                @click="swipe('dislike')">Dislike</button>
                        <button class="bg-green-500 hover:bg-green-600 text-white px-6 py-2 rounded-lg text-lg font-semibold"
                                @click="swipe('like')">Like</button>
                    </div>
                </div>
            </template>
            <template x-if="jobs.length === 0 || idx >= jobs.length">
                <div class="text-center">
                    <h2 class="text-2xl font-bold">No more jobs!</h2>
                </div>
            </template>
        </div>
        <script>
            function jobSwiper() {
                return {
                    jobs: [],
                    idx: 0,
                    fetchJobs() {
                        fetch('/jobs')
                            .then(resp => resp.json())
                            .then(data => {
                                this.jobs = data;
                                this.idx = 0;
                            });
                    },
                    currentJob() {
                        return this.jobs[this.idx] || {};
                    },
                    swipe(action) {
                        fetch('/swipe', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({job: this.currentJob(), action: action})
                        });
                        this.idx++;
                        window.scrollTo({top: 0, behavior: 'smooth'});
                    }
                }
            }
        </script>
    </body>
    </html>
    ''')

@app.route('/jobs')
def jobs_api():
    return jsonify(get_jobs())

@app.route('/swipe', methods=['POST'])
def swipe():
    data = request.json
    job = data['job']
    action = data['action']

    # Find the row in the sheet that matches the job (using a unique field, e.g., JobUrl)
    sheet = init_google_sheet()
    jobs = sheet.get_all_records()
    row_idx = None
    for idx, row in enumerate(jobs, start=2):  # Google Sheets rows are 1-indexed, first row is header
        if row.get('JobUrl') == job.get('JobUrl'):
            row_idx = idx
            break

    if row_idx:
        # Update or add a 'Swipe' column with the action
        headers = sheet.row_values(1)
        if 'Swipe' not in headers:
            sheet.update_cell(1, len(headers) + 1, 'Swipe')
            swipe_col = len(headers) + 1
        else:
            swipe_col = headers.index('Swipe') + 1
        sheet.update_cell(row_idx, swipe_col, action)
        return jsonify(success=True)

if __name__ == '__main__':
    app.run(debug=True)
