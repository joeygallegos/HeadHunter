import sys
from langchain_ollama import OllamaLLM
import random
import os
import re
import json
from langchain_ollama import ChatOllama
from langchain.schema import SystemMessage, HumanMessage

# Load config
with open("config.json", "r") as config_file:
    config = json.load(config_file)

# File path
file_path = config['JOBS_FILE']

# Read JSON data from file
with open(file_path, "r") as file:
    job_data = json.load(file)

# Select a random job description
random_job = random.choice(job_data)

# Print the random job description
print("Jobs:", len(job_data))
print(f"Job: {random_job['JobTitle']}")

def clean_text(text):
    """Cleans text by removing excessive newlines, tabs, and unnecessary whitespace."""
    # Replace escaped newline characters with actual newlines
    text = text.replace("\\n", "\n").replace("\u2022", "-")  # Bullet points
    text = text.replace("\r", " ")  # Remove carriage returns
    text = text.replace("\n", " ")  # Replace newlines with a space
    text = text.replace("\t", " ")  # Replace tabs with a space
    # Normalize spaces around newlines and collapse multiple spaces into one
    text = re.sub(r"\s*\n\s*", "\n", text)  # Ensure no extra spaces around newlines
    text = re.sub(r"\s+", " ", text)  # Collapse multiple spaces into one
    # Trim leading and trailing spaces
    text = text.strip()  
    return text

# Fix job posting string formatting
job_posting = f"JOB TITLE: {random_job['JobTitle']}\n\nJOB DESCRIPTION: {clean_text(random_job['JobDesc'])}"

# Load plaintext from Resume
resume_file = open("resume.txt", "r")
resume_text = resume_file.read()

# Set environment variables for optimization
os.environ["OLLAMA_KV_CACHE_TYPE"] = "none"  # Reduce VRAM usage
os.environ["OLLAMA_FLASH_ATTENTION"] = "true"  # Enable Flash Attention for faster inference
os.environ["OLLAMA_NUM_PARALLEL"] = "1"  # Avoid GPU overload
os.environ["OLLAMA_GPU_OVERHEAD"] = "500"  # Free up some VRAM

llm = ChatOllama(
    model="deepseek-r1:32b",
    format="json",
    max_tokens=2048,
    max_input_tokens=4096,#8192
    max_output_tokens=2048,
    temperature=0.5
)

job_posting = clean_text(job_posting)

messages = [
    SystemMessage(content=(
        "You are an AI job-matching assistant. "
        "Keep feedback entries to one sentence, max of two entries per feedback type."
        "Extract a list of important keywords from the job. Do not include the company name in the list."
        "Analyze the JOB DESCRIPTION and RESUME carefully and provide a JSON response in this format:\n\n"
        "\nREQUIRED response format:\n```json\n"
        "{\n"
        '  "match_percentage": <percentage of job match>,\n'
        '  "keywords": [<list of relevant keywords>],\n'
        '  "feedback": {\n'
        '    "positive": [],\n'
        '    "negative": []\n'
        '  }\n'
        "}\n```"
        "**If you cannot analyze the data, return this JSON:**\n"
        "```json\n"
        "{ \"error\": \"Insufficient information provided\" }\n"
        "```"
    )),
    HumanMessage(content=f"RESUME: {clean_text(resume_text)}\n\nJOB POSTING: {job_posting}")
]
# sys.exit(1)
print("Job posting tokens", len(job_posting.split()))
response = llm.invoke(messages)

# Ensure we correctly print the JSON output
try:
    result = json.loads(response.content)
    print(json.dumps(result, indent=4))
except json.JSONDecodeError:
    print("Invalid JSON response received:", response.content)