FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

VOLUME ["/app/output"]

CMD ["python3", "-m", "src.cli", "run", \
    "--input", \
    "sample_inputs/recruiter_export.csv", \
    "sample_inputs/ats_blob.json", \
    "sample_inputs/github_profiles.json", \
    "sample_inputs/recruiter_notes.txt", \
    "--output", "output/default_profiles.json", \
    "--pretty", "--summary"]
