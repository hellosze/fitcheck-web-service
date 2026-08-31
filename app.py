import os
import re
import tempfile
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from google import genai
from google.genai import types
from weasyprint import HTML

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-for-sessions")

# Static Resume URL
URL_RESUME = "https://docs.google.com/document/d/1CR3_ALCHvWhfgCTQdbqqYUD-k32LJH6M-8MBY3479O0/edit?usp=sharing"

# Get Gemini API Key from environment variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def get_gemini_client():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")
    return genai.Client(api_key=GEMINI_API_KEY)

def fetch_google_doc_text(url):
    """Converts a standard Google Doc view link into an export text stream and scrapes it."""
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
    """Scrapes raw text paragraphs from a standard website/job posting link."""
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        for script in soup(["script", "style"]):
            script.decompose()
        return "\n".join([p.get_text() for p in soup.find_all(['p', 'li', 'div']) if p.get_text().strip()])
    except Exception as e:
        return f"Could not automatically fetch text from URL due to: {str(e)}"

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        job_url = request.form.get("job_url", "").strip()
        if not job_url:
            flash("Please enter a valid Job Description URL.", "error")
            return redirect(url_for("index"))

        try:
            client = get_gemini_client()

            # 1. Fetch source documents
            raw_resume = fetch_google_doc_text(URL_RESUME)
            raw_jd = fetch_generic_url_text(job_url)

            # 2. Extract Job Title & Company
            extraction_prompt = f"""
            Analyze the following raw text scraped from a job description webpage.
            Extract the official Job Title and the Company Name.
            Return ONLY a short string in the format: "Job Title at Company Name" (e.g., "Senior Customer Success Manager at DISQO"). Do not add any extra text or pleasantries.

            --- JOB DESCRIPTION TEXT ---
            {raw_jd[:4000]}
            """

            extract_response = client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=extraction_prompt,
                config=types.GenerateContentConfig(temperature=0.0)
            )
            job_title_company = extract_response.text.strip()

            # 3. System Instructions
            resume_system_instruction = f"""
            You are an expert resume writer, technical recruiter, and executive layout designer.
            Your task is to ingest a comprehensive, multi-page resume, match it against a target Job Description, and output a raw, standalone HTML page (with embedded print-CSS styling) that will generate a perfectly spaced, print-ready, single-page PDF resume.

            Target Job Goal: {job_title_company}

            ### 1. Resume Structural Layout & Tiering Logic
            * **Highly Concise Summary:** Write a highly tailored, punchy, and ultra-concise professional summary based on STAR framework.
            * **No Standalone Skills Section:** Weave critical technical tools, languages, and methodologies directly into experience bullets.
            * **Tier 1 Experience:** Prioritize Optimera, Penske Media Corp. Use 3-5 comprehensive bullet points per role.
            * **Tier 2 Experience:** Shorten older positions (MPW Enterprises, Undertone, Frankly Media, American Media Inc, XO Group) to exactly 1 high-impact bullet point.
            * Include technical skills: Google Ad Manager (GAM & API), Prebid.js, Header Bidding, OpenRTB, VAST, Ad Verification & Brand Safety (DV, IAS, Moat), Yield & Inventory Optimization (Viewability, Brand Safety, IVT), Postman, Python (Pandas, NumPy), SQL, JavaScript/TypeScript, Docker, GitHub, Jira, Google Sheets Automation, Data Visualization, AI Prompt Engineering (ChatGPT, Google Gemini).
            * Include education: NYU Polytechnic, 2007, Masters of Science, Computer Science; City College of New York, 2004, Bachelors of Science, Computer Science.
            * Do not include tech used bullet point from Tier 2 experience.
            * Do not include leverage tools bullet point from Penske Media.

            ### 2. Strict PDF Blueprint Layout Constraints (HTML/CSS)
            Output single page style blueprint using:
            - `@page {{ size: letter; margin: 10mm 12mm 10mm 12mm; }}`
            - Base `body` font-size strictly `8.5pt` with `line-height: 1.25` web-safe sans-serif.
            - `display: block;` headers with `float: right;` spans.
            - Tight list padding (`padding-left: 12px; margin: 2px 0 4px 0;`) and minimal item margins (`margin-bottom: 2px;`).

            ### 3. Output Format Requirement
            Contain ONLY valid, pure HTML text string. Do not wrap in markdown code blocks. Start directly with <!DOCTYPE html> and end with </html>.
            """

            current_date_str = datetime.now().strftime("%B %d, %Y")
            cl_system_instruction = f"""
            ### Cover Letter Strategy & Guardrails:
            You are an expert career coach and professional resume writer. Your task is to write a highly tailored, punchy, ultra-concise cover letter.

            Inputs:
            - Candidate Name: Sze Chan
            - Candidate Contact: sze.m.chan@gmail.com | 646-269-7616
            - Target Company & Position: {job_title_company}
            - Date: {current_date_str}

            Formatting & Constraints:
            1. Standard professional header.
            2. Length: Exactly TWO paragraphs total, 3 to 6 sentences combined.
            3. Direct and punchy tone.
            4. Bridge background in technical client success to target role.
            5. Conclude with: "Thank you for your time and consideration." followed by sign-off.
            """

            user_content = f"""
            Please evaluate this target job and multi-page source resume data.

            TARGET JOB TITLE & COMPANY: {job_title_company}
            TARGET JOB DESCRIPTION: {raw_jd}
            SOURCE MULTI-PAGE RESUME: {raw_resume}
            """

            # 4. Generate Gemini Outputs
            resume_response = client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=resume_system_instruction,
                    temperature=0.2,
                )
            )

            cl_response = client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=cl_system_instruction,
                    temperature=0.3,
                )
            )

            # Clean outputs
            clean_html = resume_response.text.strip()
            if clean_html.startswith("```html"):
                clean_html = clean_html[7:]
            if clean_html.endswith("```"):
                clean_html = clean_html[:-3]
            clean_html = clean_html.strip()

            clean_cl = cl_response.text.strip()

            # 5. Store generated content temporarily in temporary files
            sanitized_job_goal = re.sub(r'[\\/*?:"<>|]', "", job_title_company)
            
            temp_dir = tempfile.gettempdir()
            resume_pdf_path = os.path.join(temp_dir, f"Resume_{sanitized_job_goal}.pdf")
            cl_pdf_path = os.path.join(temp_dir, f"Cover_Letter_{sanitized_job_goal}.pdf")

            # Convert HTML resume to PDF via WeasyPrint
            HTML(string=clean_html).write_pdf(resume_pdf_path)


            # Save Cover Letter directly as a .txt file
            cl_txt_path = os.path.join(temp_dir, f"Cover_Letter_{sanitized_job_goal}.txt")
            with open(cl_txt_path, "w", encoding="utf-8") as f:
                f.write(clean_cl)
            
            return render_template(
                "result.html",
                job_goal=job_title_company,
                resume_file=f"Resume_{sanitized_job_goal}.pdf",
                cl_file=f"Cover_Letter_{sanitized_job_goal}.txt"
            )

            # Convert Cover Letter text to simple HTML then PDF
            # cl_html_wrapper = f"<html><body style='font-family:sans-serif; font-size:10pt; line-height:1.5; margin:20mm;'><pre style='white-space:pre-wrap; font-family:inherit;'>{clean_cl}</pre></body></html>"
            # HTML(string=cl_html_wrapper).write_pdf(cl_pdf_path)

            # return render_template(
            #    "result.html",
            #    job_goal=job_title_company,
            #    resume_file=f"Resume_{sanitized_job_goal}.pdf",
            #    cl_file=f"Cover_Letter_{sanitized_job_goal}.pdf"
            #)

        except Exception as e:
            flash(f"Error generating documents: {str(e)}", "error")
            return redirect(url_for("index"))

    return render_template("index.html")

@app.route("/download/<filename>")
def download_file(filename):
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    else:
        flash("File not found or has expired.", "error")
        return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
