# HeadHunter
This suite of tools will help you apply and select jobs quicker. If you are specifically interested in being the first person to apply for jobs posted by particular companies, this might be the best way.

# Instructions
You need to create a service account in a Google cloud project and then add the email address of the service account to the Google sheet with editor access.

# Problems
Sometimes if the job description is too large, we can run out of tokes and the AI will start to hallucinate the JSON response.
Seems that the WORKING token size for the job description is around 1000 (max)

## Future Features
- Suggest jobs based on the resume data loaded into the app
- Parse user resumes to extract key skills, experiences, and preferences
- Continuously scrape job listings from multiple job boards and compile them into a unified database instead of just JSON files
- Generate a list of interview questions that might come up for a particular job based on the description
- Integrate with a lot of remote-first companies
- If you determine that the job is not fully remote, set the match_percentage to 0 and leave the feedback arrays empty
- Leave at least two positive and two negative feedback items

### Ollama Implementation
In order to locally run Ollama, use these commands:

- `ollama serve`
- `ollama list`
- `ollama rm`
- `ollama pull deepseek-r1:70b`

You can update the system environment variable OLLAMA_MODELS to be your new save path instead of the default, which is on the C drive.

## Upgrade to latest Ollama
pip install -U langchain-ollama

### TODO
