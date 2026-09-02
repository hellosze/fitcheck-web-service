import os
import re
import io
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, send_file, flash, redirect, url_for, session
from google import genai
from google.genai import types
from weasyprint import HTML

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-cloud-run")

# Static Resume URL
URL_RESUME = "https://docs.google.com/document/d/1CR3_ALCHvWhfgCTQdbqqYUD-k32LJH6M-8MBY3479O0/edit?usp=sharing"

# Get Gemini API Key from environment variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def get_gemini_client():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is not configured.")
    return genai.Client(api_key=GEMINI_API_KEY)

def fetch_google_doc_text(url):
    """Converts Google Doc view link to export text stream."""
    doc_id_match = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if not doc_id_match:
        raise ValueError("Invalid Google Doc URL structure.")
    export_url = f"https://docs.google.com/document/d/{doc_id_match.group(1)}/export?format=txt"
    response = requests.get(export_url, timeout=10)
    if response.status_code == 200:
        return response.text
    else:
        res = requests.get(url, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        return soup.get_text(separator='\n')

def fetch_generic_url_text(url):
    """Scrapes raw text from job posting link."""
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        for script in soup(["script", "style"]):
            script.decompose()
        return "\n".join([p.get_text() for p in soup.find_all(['p', 'li', 'div']) if p.get_text().strip()])
    except Exception as e:
        return f"Could not automatically fetch text from URL due to: {str(e)}"

# In-memory storage cache for stateless server execution
GENERATED_CACHE = {}

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        job_url = request.form.get("job_url", "").strip()
        if not job_url:
            flash("Please enter a valid Job Description URL.", "error")
            return redirect(url_for("index"))

        try:
            client = get_gemini_client()

            raw_resume = fetch_google_doc_text(URL_RESUME)
            raw_jd = fetch_generic_url_text(job_url)
            current_date_str = datetime.now().strftime("%B %d, %Y")

            # Consolidated prompt in 1 single Gemini call to prevent 503 errors and speed up execution
            combined_prompt = f"""
You are an expert resume writer, technical recruiter, and executive layout designer.
Analyze the target Job Description and Source Resume below. Perform the following tasks:

Task 1: Extract official Job Title and Company Name in format "Job Title at Company Name".
Task 2: Generate a single-page standalone HTML resume with embedded print CSS styling (@page {{ size: letter; margin: 10mm 12mm; }}).
Task 3: Generate a punchy, 2-paragraph cover letter for candidate Sze Chan (sze.m.chan@gmail.com | 646-269-7616) dated {current_date_str}.

You MUST strictly format your output with these EXACT delimiter tags:
===JOB_GOAL===
[Job Title at Company]
===RESUME_HTML===
[Full standalone HTML text starting with <!DOCTYPE html> and ending with </html> without markdown code blocks]
===COVER_LETTER===
[Full Cover Letter plain text]

--- TARGET JOB DESCRIPTION ---
{raw_jd[:4000]}

--- SOURCE RESUME ---
{raw_resume}
"""

            # Single API call using high-availability model
            response = client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=combined_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2
                )
            )

            output = response.text
            job_goal = output.split("===JOB_GOAL===")[1].split("===RESUME_HTML===")[0].strip()
            clean_html = output.split("===RESUME_HTML===")[1].split("===COVER_LETTER===")[0].strip()
            clean_cl = output.split("===COVER_LETTER===")[1].strip()

            # Clean markdown fences if model outputs them
            if clean_html.startswith("```html"):
                clean_html = clean_html[7:]
            if clean_html.endswith("```"):
                clean_html = clean_html[:-3]
            clean_html = clean_html.strip()

            # Generate PDF in-memory buffer
            pdf_buffer = io.BytesIO()
            HTML(string=clean_html).write_pdf(pdf_buffer)
            pdf_buffer.seek(0)

            # Generate a session-scoped unique ID to store artifacts in-memory
            session_id = datetime.now().strftime("%Y%m%d%H%M%S")
            sanitized_job_goal = re.sub(r'[\\/*?:"<>|]', "", job_goal)

            GENERATED_CACHE[session_id] = {
                "pdf_data": pdf_buffer.getvalue(),
                "cl_data": clean_cl.encode("utf-8"),
                "job_goal": job_goal,
                "sanitized_goal": sanitized_job_goal
            }

            session["current_id"] = session_id

            return render_template(
                "result.html",
                job_goal=job_goal,
                session_id=session_id
            )

        except Exception as e:
            flash(f"Error generating documents: {str(e)}", "error")
            return redirect(url_for("index"))

    return render_template("index.html")

@app.route("/download/resume/<session_id>")
def download_resume(session_id):
    cache = GENERATED_CACHE.get(session_id)
    if not cache:
        flash("Session expired or file not found. Please generate again.", "error")
        return redirect(url_for("index"))

    return send_file(
        io.BytesIO(cache["pdf_data"]),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"Sze Chan - Resume - {cache['sanitized_goal']}.pdf"
    )

@app.route("/download/cover-letter/<session_id>")
def download_cover_letter(session_id):
    cache = GENERATED_CACHE.get(session_id)
    if not cache:
        flash("Session expired or file not found. Please generate again.", "error")
        return redirect(url_for("index"))

    return send_file(
        io.BytesIO(cache["cl_data"]),
        mimetype="text/plain",
        as_attachment=True,
        download_name=f"Sze Chan - Cover Letter - {cache['sanitized_goal']}.txt"
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
