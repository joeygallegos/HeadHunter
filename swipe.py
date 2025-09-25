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

# Normalize curly quotes to straight so regexes are reliable, but keep 1:1 length
def _normalize(s: str) -> str:
    return (s.replace("’", "'")
             .replace("‘", "'")
             .replace("“", '"')
             .replace("”", '"'))

def highlight_as_you_will(job_title: str, job_desc: str) -> str:
    if not job_title or not job_desc:
        return job_desc or ""

    text = job_desc
    norm = _normalize(text)

    # Sentence-ish end: stop at ., !, or ? (optionally followed by closing quote/paren) then space or end
    END = r'[.!?](?:["\')\]]+)?(?:\s|$)'

    # One pattern per rule; we’ll only take the first match for each
    patterns = [
        # "As a ... you will ..." — keep it bounded to the sentence
        rf'\bAs\s+a\b[^.!?]{{0,300}}?\byou\s+will\b[^.!?]*?{END}',

        # Headed sections; avoid Sr./Jr. false stops by checking the char before the dot
        rf'\bAbout the Role:\s*[^.!?]*?(?<!S)(?<!J)\.{1}(?:\s|$)',
        rf"\bWhat You(?:'|’)ll Do:\s*[^.!?]*?(?<!S)(?<!J)\.{1}(?:\s|$)",

        rf'\bIn this role\b[^.!?]*?{END}',
        rf'\bYou will own\b[^.!?]*?{END}',
        rf'\bYou will be responsible for\b[^.!?]*?{END}',
        rf'\bYou will lead\b[^.!?]*?{END}',
        rf'\bYou will manage\b[^.!?]*?{END}',
        rf'\bAs part of this\b[^.!?]*?{END}',
        rf'\bThis role will be responsible for\b[^.!?]*?{END}',
        rf'\bWe seek a\b[^.!?]*?{END}',
    ]

    flags = re.IGNORECASE

    # Collect at most one span per pattern on the normalized text
    spans = []
    for pat in patterns:
        m = re.search(pat, norm, flags)
        if m:
            start, end = m.span()
            spans.append((start, end))

    if not spans:
        # Nothing matched; just escape entire text for safety
        return html.escape(text)

    # Sort and de-overlap: keep earlier, longer spans first where they collide
    spans.sort()
    dedup = []
    last_end = -1
    for s, e in spans:
        if s >= last_end:
            dedup.append((s, e))
            last_end = e
        else:
            # Overlap: keep whichever span is larger (prevents "stepping")
            prev_s, prev_e = dedup[-1]
            if (e - s) > (prev_e - prev_s):
                dedup[-1] = (s, e)
                last_end = e
            # else keep previous; drop this one

    # Render once: escape everything, wrap matched slices
    out = []
    cursor = 0
    for s, e in dedup:
        if cursor < s:
            out.append(html.escape(text[cursor:s]))
        out.append(f"<mark class='bg-yellow-200'>{html.escape(text[s:e])}</mark>")
        cursor = e
    if cursor < len(text):
        out.append(html.escape(text[cursor:]))

    return ''.join(out)

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
                        :class="`text-white px-2 rounded ${levelClass()}`">
                        </span>
                    </p>
                    <p class="mb-1"><span class="font-semibold">Pay:</span> <span x-text="currentJob().JobPay || ''"></span></p>
                    <p class="mb-1"><span class="font-semibold">Discovery Date:</span> <span x-text="currentJob().DiscoveryDate || ''"></span></p>
                    <p class="mb-1"><span class="font-semibold">ID:</span> <span x-text="currentJob().JobID || ''"></span></p>
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
                    levelClass() {
                        const lvl = (this.currentJob().JobLevel || '').toLowerCase();
                        const map = {
                            architect: 'bg-red-500',
                            senior:    'bg-orange-400',
                            leader:    'bg-gray-400',    // <- use gray-*, not slate-* on Tailwind v2
                            junior:    'bg-blue-400',
                            unknown:   'bg-gray-400',
                        };
                        return map[lvl] || 'bg-gray-300';
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
