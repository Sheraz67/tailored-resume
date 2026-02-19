#!/usr/bin/env python3
"""Resume Tailor Web App — Flask backend."""

import io
import json
import os
import re
import tempfile
from datetime import datetime
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request, jsonify, send_file
from fpdf import FPDF
import anthropic

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB max upload


# ─────────────────────────────────────────────
# Resume file parsing
# ─────────────────────────────────────────────

def parse_docx(file_bytes: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(file_bytes))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def parse_pdf(file_bytes: bytes) -> str:
    from PyPDF2 import PdfReader
    reader = PdfReader(io.BytesIO(file_bytes))
    text_parts = []
    for page in reader.pages:
        t = page.extract_text()
        if t:
            text_parts.append(t)
    return "\n".join(text_parts)


def parse_txt(file_bytes: bytes) -> str:
    return file_bytes.decode("utf-8", errors="replace")


PARSERS = {
    ".docx": parse_docx,
    ".pdf": parse_pdf,
    ".txt": parse_txt,
}


# ─────────────────────────────────────────────
# Claude API call
# ─────────────────────────────────────────────

def tailor_resume(api_key: str, resume_text: str, prompt_text: str, jd_text: str) -> dict:
    """Call Claude to tailor the resume. Returns structured JSON."""

    client = anthropic.Anthropic(api_key=api_key)

    system_msg = prompt_text.strip()

    user_msg = f"""Here are the two inputs:

=== CANDIDATE RESUME ===
{resume_text}

=== JOB DESCRIPTION ===
{jd_text}

=== INSTRUCTIONS ===
Apply every phase from your system prompt to these inputs.

IMPORTANT: Return ONLY the tailored resume as a JSON object with this exact structure (no change log, no interview prep — just the resume):

```json
{{
  "name": "Candidate Name",
  "title": "Tailored Job Title",
  "contact": "Location | email",
  "summary": "The full summary paragraph",
  "skills": [
    {{"category": "Category Name", "items": "skill1, skill2, skill3"}}
  ],
  "experience": [
    {{
      "job_title": "Title",
      "company": "Company Name",
      "context": "Domain context tag",
      "dates": "MM/YYYY - MM/YYYY",
      "location": "City, ST or Remote",
      "bullets": ["bullet 1", "bullet 2"]
    }}
  ],
  "education": {{
    "degree": "Degree Name",
    "school": "School Name",
    "dates": "MM/YYYY - MM/YYYY",
    "location": "City, ST"
  }}
}}
```

Return ONLY the JSON object, no markdown code fences, no extra text. Just pure JSON.
"""

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=8000,
        messages=[
            {"role": "user", "content": f"{system_msg}\n\n{user_msg}"}
        ],
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if Claude wraps them anyway
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    return json.loads(raw)


# ─────────────────────────────────────────────
# PDF generation
# ─────────────────────────────────────────────

class ResumePDF(FPDF):
    BLUE = (44, 95, 138)
    DARK = (26, 26, 26)
    BODY = (51, 51, 51)
    GRAY = (102, 102, 102)

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=15)

    def header(self):
        pass  # no header

    def section_header(self, text):
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(*self.BLUE)
        self.cell(0, 8, text.upper(), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*self.BLUE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def body_text(self, text, size=10):
        self.set_font("Helvetica", "", size)
        self.set_text_color(*self.BODY)
        self.multi_cell(0, 5, text)

    def bullet(self, text):
        x = self.get_x()
        self.set_font("Helvetica", "", 9.5)
        self.set_text_color(*self.BODY)
        indent = 8
        self.set_x(x + indent)
        # Replace problematic characters
        clean = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\u2022", "-").replace("\u2026", "...")
        self.multi_cell(self.w - self.r_margin - self.get_x(), 4.5, f"- {clean}")
        self.ln(0.5)


def generate_pdf(data: dict) -> bytes:
    pdf = ResumePDF()
    pdf.add_page()
    pdf.set_margins(18, 15, 18)
    pdf.set_y(15)

    # Name
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*ResumePDF.DARK)
    name = data.get("name", "").encode("latin-1", "replace").decode("latin-1")
    pdf.cell(0, 10, name, align="C", new_x="LMARGIN", new_y="NEXT")

    # Title
    pdf.set_font("Helvetica", "", 13)
    pdf.set_text_color(*ResumePDF.BLUE)
    pdf.cell(0, 7, data.get("title", ""), align="C", new_x="LMARGIN", new_y="NEXT")

    # Contact
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*ResumePDF.GRAY)
    pdf.cell(0, 6, data.get("contact", ""), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Summary
    pdf.section_header("Summary")
    pdf.body_text(data.get("summary", ""))
    pdf.ln(3)

    # Skills
    pdf.section_header("Skills")
    for skill in data.get("skills", []):
        cat = skill.get("category", "")
        items = skill.get("items", "")
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*ResumePDF.BODY)
        label = f"{cat}: "
        pdf.cell(pdf.get_string_width(label) + 1, 5, label)
        pdf.set_font("Helvetica", "", 9.5)
        remaining = pdf.w - pdf.r_margin - pdf.get_x()
        pdf.multi_cell(remaining, 4.5, items)
        pdf.ln(1)
    pdf.ln(2)

    # Experience
    pdf.section_header("Experience")
    for job in data.get("experience", []):
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*ResumePDF.DARK)
        pdf.cell(0, 6, job.get("job_title", ""), new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*ResumePDF.BLUE)
        company_ctx = f"{job.get('company', '')} -- {job.get('context', '')}"
        pdf.cell(pdf.get_string_width(company_ctx) + 2, 5, company_ctx)
        pdf.set_text_color(*ResumePDF.GRAY)
        meta = f"  |  {job.get('dates', '')}  |  {job.get('location', '')}"
        pdf.cell(0, 5, meta, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

        for b in job.get("bullets", []):
            pdf.bullet(b)
        pdf.ln(3)

    # Education
    edu = data.get("education", {})
    if edu:
        pdf.section_header("Education")
        pdf.set_font("Helvetica", "B", 10.5)
        pdf.set_text_color(*ResumePDF.BODY)
        pdf.cell(0, 6, edu.get("degree", ""), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*ResumePDF.GRAY)
        edu_meta = f"{edu.get('school', '')}  |  {edu.get('dates', '')}  |  {edu.get('location', '')}"
        pdf.cell(0, 5, edu_meta)

    return pdf.output()


# ─────────────────────────────────────────────
# JD scraping helpers
# ─────────────────────────────────────────────

PLATFORM_DOMAINS = {
    "linkedin.com": "LinkedIn",
    "indeed.com": "Indeed",
    "glassdoor.com": "Glassdoor",
    "ziprecruiter.com": "ZipRecruiter",
    "monster.com": "Monster",
    "dice.com": "Dice",
    "lever.co": "Lever",
    "greenhouse.io": "Greenhouse",
    "workday.com": "Workday",
    "myworkdayjobs.com": "Workday",
    "smartrecruiters.com": "SmartRecruiters",
    "angel.co": "AngelList",
    "wellfound.com": "Wellfound",
    "builtin.com": "Built In",
    "simplyhired.com": "SimplyHired",
    "careerbuilder.com": "CareerBuilder",
    "welcometothejungle.com": "Welcome to the Jungle",
}

# All known platform names (lowercase) for filtering from titles
_PLATFORM_NAMES_LOWER = {v.lower() for v in PLATFORM_DOMAINS.values()} | {
    "linkedin", "indeed", "indeed.com", "glassdoor", "glassdoor.com",
    "otta", "welcome to the jungle",
}


def _is_platform_part(text):
    """Check if a title part is a platform name (not a real company)."""
    t = text.lower().strip()
    for pn in _PLATFORM_NAMES_LOWER:
        if pn in t:
            return True
    return False


def extract_job_metadata(url, title, text):
    """Detect platform from domain and parse company/position from page title."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    # Detect platform
    platform = "Other"
    for domain_key, platform_name in PLATFORM_DOMAINS.items():
        if domain_key in domain:
            platform = platform_name
            break

    company = ""
    position = ""

    if title:
        # LinkedIn: "Job Title at Company | LinkedIn"
        # Indeed: "Job Title - Company - Location | Indeed.com"
        # Glassdoor: "Company hiring Job Title in Location | Glassdoor"
        # WTTJ: "Company - Role | Welcome to the Jungle"

        if platform == "LinkedIn":
            m = re.match(r"^(.+?)\s+at\s+(.+?)(?:\s*\||\s*[-–]|\s*$)", title)
            if m:
                position = m.group(1).strip()
                company = m.group(2).strip()
        elif platform == "Indeed":
            parts = re.split(r"\s*[-–]\s*", title)
            if len(parts) >= 2:
                position = parts[0].strip()
                company = parts[1].strip()
        elif platform == "Glassdoor":
            m = re.match(r"^(.+?)\s+hiring\s+(.+?)(?:\s+in\s+|\s*\|)", title)
            if m:
                company = m.group(1).strip()
                position = m.group(2).strip()
        elif platform == "Welcome to the Jungle":
            # "Company - Role | Welcome to the Jungle (formerly Otta)"
            # Split on | first, take everything before the platform name
            main = re.split(r"\s*\|\s*", title)[0]
            parts = re.split(r"\s*[-–]\s*", main, maxsplit=1)
            if len(parts) >= 2:
                company = parts[0].strip()
                position = parts[1].strip()
            elif len(parts) == 1:
                position = parts[0].strip()

        # Generic fallback
        if not company and not position:
            # Split on | first to separate "content | platform branding"
            pipe_parts = re.split(r"\s*\|\s*", title)
            # Filter out parts that are platform names
            content_parts = [p.strip() for p in pipe_parts if not _is_platform_part(p)]
            if not content_parts:
                content_parts = [p.strip() for p in pipe_parts]

            # Use just the first content part (before any |), split on dash for company/role
            main = content_parts[0]
            sub = re.split(r"\s*[-–]\s*", main, maxsplit=1)
            if len(sub) >= 2:
                company = sub[0].strip()
                position = sub[1].strip()
            elif len(sub) == 1:
                position = sub[0].strip()

    return {
        "platform": platform,
        "company": company,
        "position": position,
        "url": url,
    }


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scrape-jd", methods=["POST"])
def api_scrape_jd():
    """Scrape a job description from a URL using Apify website-content-crawler."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided."}), 400

        apify_token = data.get("apify_token", "").strip()
        url = data.get("url", "").strip()

        if not apify_token:
            return jsonify({"error": "Apify API token is required."}), 400
        if not url:
            return jsonify({"error": "Job URL is required."}), 400

        # Call Apify website-content-crawler actor
        actor_id = "apify~website-content-crawler"
        run_url = f"https://api.apify.com/v2/acts/{actor_id}/runs"

        run_input = {
            "startUrls": [{"url": url}],
            "maxCrawlPages": 1,
            "crawlerType": "playwright:firefox",
            "maxConcurrency": 1,
            "proxyConfiguration": {"useApifyProxy": True},
        }

        # Start the actor run synchronously (wait for finish)
        resp = requests.post(
            run_url,
            params={"token": apify_token, "waitForFinish": 120},
            json=run_input,
            timeout=180,
        )

        if resp.status_code == 401:
            return jsonify({"error": "Invalid Apify API token."}), 401
        if not resp.ok:
            return jsonify({"error": f"Apify API error: {resp.status_code} {resp.text[:200]}"}), 502

        run_data = resp.json().get("data", {})
        run_status = run_data.get("status")

        if run_status != "SUCCEEDED":
            return jsonify({"error": f"Apify crawl did not succeed (status: {run_status}). Try again."}), 502

        # Fetch results from the default dataset
        dataset_id = run_data.get("defaultDatasetId")
        if not dataset_id:
            return jsonify({"error": "No dataset returned from Apify."}), 502

        items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        items_resp = requests.get(
            items_url,
            params={"token": apify_token, "format": "json"},
            timeout=30,
        )

        if not items_resp.ok:
            return jsonify({"error": "Failed to fetch crawl results."}), 502

        items = items_resp.json()
        if not items:
            return jsonify({"error": "No content was extracted from the URL. The page may require login."}), 404

        item = items[0]
        page_text = item.get("text", "")
        page_title = item.get("metadata", {}).get("title", "") or item.get("title", "")

        if not page_text:
            return jsonify({"error": "Page was loaded but no text content was found."}), 404

        metadata = extract_job_metadata(url, page_title, page_text)

        return jsonify({
            "success": True,
            "text": page_text,
            "title": page_title,
            "metadata": metadata,
        })

    except requests.Timeout:
        return jsonify({"error": "Apify request timed out. Try again."}), 504
    except Exception as e:
        return jsonify({"error": f"Scraping failed: {str(e)}"}), 500


@app.route("/api/tailor", methods=["POST"])
def api_tailor():
    try:
        api_key = request.form.get("api_key", "").strip()
        if not api_key:
            return jsonify({"error": "Anthropic API key is required."}), 400

        jd_text = request.form.get("jd", "").strip()
        resume_text_direct = request.form.get("resume_text", "").strip()

        # Parse prompt from file or text
        prompt_text = request.form.get("prompt", "").strip()
        if "prompt_file" in request.files:
            pf = request.files["prompt_file"]
            if pf.filename:
                pext = os.path.splitext(pf.filename)[1].lower()
                if pext not in PARSERS:
                    return jsonify({"error": f"Unsupported prompt file type: {pext}. Use .txt, .docx, or .pdf"}), 400
                prompt_text = PARSERS[pext](pf.read())

        if not prompt_text:
            return jsonify({"error": "Tailoring prompt is required. Upload a file or paste text."}), 400
        if not jd_text:
            return jsonify({"error": "Job description is required."}), 400

        # Parse resume from file or text
        resume_text = ""
        if "resume_file" in request.files:
            f = request.files["resume_file"]
            if f.filename:
                ext = os.path.splitext(f.filename)[1].lower()
                if ext not in PARSERS:
                    return jsonify({"error": f"Unsupported file type: {ext}. Use .docx, .pdf, or .txt"}), 400
                resume_text = PARSERS[ext](f.read())

        if not resume_text and resume_text_direct:
            resume_text = resume_text_direct

        if not resume_text:
            return jsonify({"error": "Please upload a resume file or paste resume text."}), 400

        # Call Claude
        result = tailor_resume(api_key, resume_text, prompt_text, jd_text)
        return jsonify({"success": True, "data": result})

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Failed to parse AI response as JSON. Try again. Details: {str(e)}"}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "Invalid API key. Please check your Anthropic API key."}), 401
    except anthropic.RateLimitError:
        return jsonify({"error": "Rate limited. Please wait a moment and try again."}), 429
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {str(e)}"}), 500


@app.route("/api/answer-questions", methods=["POST"])
def api_answer_questions():
    """Answer job application questions using JD + resume context."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided."}), 400

        api_key = data.get("api_key", "").strip()
        questions = data.get("questions", "").strip()
        jd_text = data.get("jd", "").strip()
        resume_json = data.get("resume", {})

        if not api_key:
            return jsonify({"error": "API key is required."}), 400
        if not questions:
            return jsonify({"error": "Please paste at least one question."}), 400
        if not jd_text:
            return jsonify({"error": "Job description context is missing."}), 400

        # Rebuild a readable resume summary from the structured data
        resume_lines = []
        resume_lines.append(f"Name: {resume_json.get('name', '')}")
        resume_lines.append(f"Title: {resume_json.get('title', '')}")
        resume_lines.append(f"\nSummary:\n{resume_json.get('summary', '')}")
        resume_lines.append("\nSkills:")
        for s in resume_json.get("skills", []):
            resume_lines.append(f"  {s.get('category', '')}: {s.get('items', '')}")
        resume_lines.append("\nExperience:")
        for job in resume_json.get("experience", []):
            resume_lines.append(f"\n  {job.get('job_title', '')} at {job.get('company', '')} ({job.get('dates', '')})")
            for b in job.get("bullets", []):
                resume_lines.append(f"    - {b}")
        edu = resume_json.get("education", {})
        if edu:
            resume_lines.append(f"\nEducation: {edu.get('degree', '')} — {edu.get('school', '')}")
        resume_text = "\n".join(resume_lines)

        client = anthropic.Anthropic(api_key=api_key)

        system_prompt = """You are an expert job application assistant. You help candidates write compelling,
authentic answers to job application questions. You have deep context about:
1. The candidate's background (their tailored resume)
2. The specific job they are applying for (the job description)

Rules:
- Write answers in FIRST PERSON as the candidate
- Keep answers concise but substantive (3-6 sentences per question unless it clearly needs more)
- Ground every answer in REAL experience from the resume — never fabricate
- Mirror the tone and keywords from the job description naturally
- Show enthusiasm for the specific role and company
- If a question asks about something not covered in the resume, craft an honest answer
  that pivots to relevant strengths rather than making things up
- For salary questions, suggest the candidate research market rates rather than giving a number
- For "why this company" questions, reference specific things from the JD that align with the candidate's experience"""

        user_msg = f"""Here is the candidate's resume:

{resume_text}

Here is the job description they are applying to:

{jd_text}

Please answer each of the following application questions. Format your response as a JSON array where each
element has "question" (the original question) and "answer" (your crafted response).

Questions:
{questions}

Return ONLY a JSON array, no markdown code fences, no extra text. Example format:
[{{"question": "Why do you want this role?", "answer": "Your answer here..."}}]"""

        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4000,
            messages=[
                {"role": "user", "content": f"{system_prompt}\n\n{user_msg}"}
            ],
        )

        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

        answers = json.loads(raw)
        return jsonify({"success": True, "answers": answers})

    except json.JSONDecodeError as e:
        return jsonify({"error": f"Failed to parse AI response. Try again. Details: {str(e)}"}), 500
    except anthropic.AuthenticationError:
        return jsonify({"error": "Invalid API key."}), 401
    except anthropic.RateLimitError:
        return jsonify({"error": "Rate limited. Please wait and try again."}), 429
    except Exception as e:
        return jsonify({"error": f"Something went wrong: {str(e)}"}), 500


@app.route("/api/download-pdf", methods=["POST"])
def api_download_pdf():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No resume data provided."}), 400

        pdf_bytes = generate_pdf(data)
        name_slug = data.get("name", "resume").replace(" ", "_")
        filename = f"{name_slug}.pdf"

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        return jsonify({"error": f"PDF generation failed: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
