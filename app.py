import os
import re
import tempfile
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from flask import Flask, request, render_template_string, send_file, flash, redirect, url_for
from weasyprint import HTML
from google import genai
from google.genai import types

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key")

# Retrieve API Key from Cloud Run Environment Variables
API_KEY = os.environ.get("GEMINI_API_KEY")
URL_RESUME = "https://docs.google.com/document/d/1CR3_ALCHvWhfgCTQdbqqYUD-k32LJH6M-8MBY3479O0/edit?usp=sharing"

# ==============================================================================
# HELPER FUNCTIONS TO FETCH TEXT DATA
# ==============================================================================
def fetch_google_doc_text(url):
    doc_id_match = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if not doc_id_match:
        raise ValueError("Invalid Google Doc URL structure.")
    export_url = f"https://docs.google.com/document/d/{doc_id_match.group(1)}/export?format=txt"
    response = requests.get(export_url)
    if response.status_code == 200:
        return response.text
    else:
        res = requests.get(url)
        soup = BeautifulSoup(res.text, 'html.parser')
        return soup.get_text(separator='\n')

def fetch_generic_url_text(url):
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(res.text, 'html.parser')
        for script in soup(["script", "style"]):
            script.decompose()
        return "\n".join([p.get_text() for p in soup.find_all(['p', 'li', 'div']) if p.get_text().strip()])
    except Exception as e:
        return f"Could not automatically fetch text from URL due to: {str(e)}"

# ==============================================================================
# HTML TEMPLATES FOR CLOUD RUN WEB INTERFACE
# ==============================================================================
INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Resume & Cover Letter Generator</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; }
        input[type="text"] { width: 100%; padding: 10px; margin: 10px 0; box-sizing: border-box; }
        button { padding: 10px 20px; background-color: #007bff; color: white; border: none; cursor: pointer; margin-right: 10px; }
        button:hover { background-color: #0056b3; }
        .form-group { margin-bottom: 20px; }
    </style>
</head>
<body>
    <h2>Generate Tailored Resume & Cover Letter</h2>
    <form action="/generate" method="post">
        <div class="form-group">
            <label for="job_url">Job Description URL:</label>
            <input type="text" id="job_url" name="job_url" placeholder="https://example.com/job-posting" required>
        </div>
        <button type="submit" name="action" value="resume">Generate Resume (PDF)</button>
        <button type="submit" name="action" value="cover_letter_pdf">Generate Cover Letter (PDF)</button>
        <button type="submit" name="action" value="cover_letter_txt">Generate Cover Letter (TXT)</button>
    </form>
</body>
</html>
"""

# ==============================================================================
# FLASK ROUTES
# ==============================================================================
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

@app.route("/generate", methods=["POST"])
def generate():
    job_url = request.form.get("job_url", "").strip()
    action = request.form.get("action")

    if not job_url:
        return "Please provide a valid Job URL", 400

    # Fetch Data
    raw_resume = fetch_google_doc_text(URL_RESUME)
    raw_jd = fetch_generic_url_text(job_url)

    # Initialize Gemini
    client = genai.Client(api_key=API_KEY)

    # Extract Title/Company
    extraction_prompt = f"""
    Analyze the following raw text scraped from a job description webpage.
    Extract the official Job Title and the Company Name.
    Return ONLY a short string in the format: "Job Title at Company Name". Do not add any extra text.

    --- JOB DESCRIPTION TEXT ---
    {raw_jd[:4000]}
    """
    extract_response = client.models.generate_content(
        model='gemini-3.5-flash-lite',
        contents=extraction_prompt,
        config=types.GenerateContentConfig(temperature=0.0)
    )
    JOB_TITLE_COMPANY = extract_response.text.strip()
    sanitized_job_goal = re.sub(r'[\\/*?:"<>|]', "", JOB_TITLE_COMPANY)

    user_content = f"""
    Please evaluate this target job and multi-page source resume data.

    ---
    ### TARGET JOB TITLE & COMPANY:
    {JOB_TITLE_COMPANY}

    ---
    ### TARGET JOB DESCRIPTION:
    {raw_jd}

    ---
    ### SOURCE MULTI-PAGE RESUME:
    {raw_resume}
    """

    # --- RESUME GENERATION ---
    if action == "resume":
        resume_system_instruction = f"""
        You are an expert resume writer, technical recruiter, and executive layout designer.
        Your task is to ingest a comprehensive, multi-page resume, match it against a target Job Description, and output a raw, standalone HTML page (with embedded print-CSS styling) that will generate a perfectly spaced, print-ready, single-page PDF resume.

        Target Job Goal: {JOB_TITLE_COMPANY}

        ### 1. Resume Structural Layout & Tiering Logic
        * **Highly Concise Summary:** STAR framework aligned summary.
        * **No Standalone Skills Section:** Weave keywords into experience.
        * **No linkedin links.
        * **Tier 1 Experience:** Prioritize Optimera, Penske Media Corp. (3-5 bullets each).
        * **Tier 2 Experience:** MPW Enterprises, Undertone, Frankly Media, American Media Inc, XO Group (1 bullet each).
        * Include technical skills: Google Ad Manager (GAM & API), Prebid.js, Header Bidding, OpenRTB, VAST, Ad Verification, Python, SQL, JavaScript, Docker, Gemini.
        * Include education: NYU Polytechnic (MS CS, 2007), CCNY (BS CS, 2004).

        ### 2. Strict PDF Blueprint Layout Constraints (HTML/CSS)
        - `@page {{ size: letter; margin: 10mm 12mm 10mm 12mm; }}`
        - Set base `body` font-size strictly to `8.5pt` with `line-height: 1.25`.

        ### 3. Output Format Requirement
        Your response must contain ONLY valid, pure HTML text string starting directly with <!DOCTYPE html> and ending with </html>.
        """
        resume_response = client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=resume_system_instruction,
                temperature=0.2,
            )
        )
        clean_html = resume_response.text.strip()
        if clean_html.startswith("```html"):
            clean_html = clean_html[7:]
        if clean_html.endswith("```"):
            clean_html = clean_html[:-3]

        # Render PDF to memory/tempfile and stream to client
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
            HTML(string=clean_html.strip()).write_pdf(tmp_pdf.name)
            return send_file(
                tmp_pdf.name,
                as_attachment=True,
                download_name=f"Sze Chan - {sanitized_job_goal}.pdf",
                mimetype="application/pdf"
            )

    # --- COVER LETTER GENERATION ---
    elif action in ["cover_letter_pdf", "cover_letter_txt"]:
        current_date_str = datetime.now().strftime("%B %d, %Y")
        cl_system_instruction = f"""
        You are an expert career coach. Write a tailored, 2-paragraph cover letter based on candidate profile and target role.

        Inputs:
        - Candidate Name: Sze Chan
        - Contact: sze.m.chan@gmail.com | 646-269-7616
        - Target Company: {JOB_TITLE_COMPANY}
        - Date: {current_date_str}

        Keep body text strictly TWO paragraphs (3 to 6 sentences total).
        End with: "Thank you for your time and consideration."
        """
        cl_response = client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=cl_system_instruction,
                temperature=0.3,
            )
        )
        clean_cl = cl_response.text.strip()

        if action == "cover_letter_txt":
            with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as tmp_txt:
                tmp_txt.write(clean_cl)
                tmp_txt.flush()
                return send_file(
                    tmp_txt.name,
                    as_attachment=True,
                    download_name=f"Sze Chan - Cover Letter - {sanitized_job_goal}.txt",
                    mimetype="text/plain"
                )

        elif action == "cover_letter_pdf":
            # Wrap plain text in minimal HTML for proper WeasyPrint PDF conversion
            cl_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <style>
                    @page {{ size: letter; margin: 20mm; }}
                    body {{ font-family: Arial, sans-serif; font-size: 11pt; line-height: 1.5; white-space: pre-wrap; }}
                </style>
            </head>
            <body>{clean_cl}</body>
            </html>
            """
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
                HTML(string=cl_html).write_pdf(tmp_pdf.name)
                return send_file(
                    tmp_pdf.name,
                    as_attachment=True,
                    download_name=f"Sze Chan - Cover Letter - {sanitized_job_goal}.pdf",
                    mimetype="application/pdf"
                )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
