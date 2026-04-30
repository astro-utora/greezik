"""
Step 1 — Find the Best-Matched Resume
Reads Job_Description.txt, scans all .docx resumes in /resumes,
and picks the resume that best matches the job description.

Scoring strategy:
  1. Job title analysis  — terms in the title get highest weight (they define the role).
  2. Section-aware parsing — required skills > preferred/nice-to-have > general text.
  3. IDF discrimination   — terms found in every resume are down-weighted; rare terms
                            (e.g. "ruby", "rails") are up-weighted.
  4. Filename tag matching — curated skill labels in parentheses are a strong signal,
                            weighted proportionally to the term's importance.
  5. Expanded stop words  — generic engineering verbs/nouns filtered out so they
                            don't inflate scores uniformly across all resumes.

Uses a text cache to avoid re-reading unchanged .docx files.
"""

import re
import sys
import json
import math
from pathlib import Path
from docx import Document

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from utils.paths import resumes_dir as _resumes_dir  # noqa: E402

STEPS_DIR = Path(__file__).resolve().parent
JOB_DESC_PATH = BASE_DIR / "bid" / "Job_Description.txt"
RESUMES_DIR = _resumes_dir()
OUTPUT_PATH = STEPS_DIR / "_best_resume.txt"
CACHE_PATH = STEPS_DIR / "_resume_cache.json"

# ── Multi-word tech phrases (matched as whole tokens) ──
TECH_PHRASES = [
    # .NET ecosystem
    '.net core', '.net 8', 'asp.net core', 'asp.net mvc', 'asp.net web api',
    'entity framework', 'entity framework core', 'blazor',
    # Azure
    'azure functions', 'azure service bus', 'azure logic apps', 'azure devops',
    'azure app service', 'azure active directory', 'azure ad',
    'azure kubernetes', 'azure blob storage', 'application insights', 'azure pipelines',
    # Power Platform / Dynamics
    'power platform', 'power apps', 'power automate', 'power bi',
    'dynamics 365', 'dynamics crm',
    # Databases
    'sql server', 'cosmos db', 'active record', 'activerecord',
    # Ruby ecosystem
    'ruby on rails', 'rspec', 'minitest', 'sidekiq', 'active job',
    'active storage', 'action mailer', 'action cable', 'rack middleware',
    # Python ecosystem
    'fast api', 'fastapi', 'flask', 'django',
    # ML / AI
    'machine learning', 'deep learning', 'natural language processing',
    'large language model', 'computer vision', 'generative ai', 'agentic ai',
    # JS frameworks
    'react native', 'react.js', 'vue.js', 'node.js', 'next.js', 'angular',
    'express.js', 'nuxt.js',
    # Java ecosystem
    'spring boot', 'spring cloud', 'quarkus',
    # Go ecosystem
    'go', 'golang',
    # Svelte / HTMX
    'svelte', 'htmx',
    # GCP services
    'cloud run', 'cloud sql', 'vertex ai',
    # CI/CD
    'ci/cd', 'github actions', 'azure pipelines',
    # API styles
    'rest api', 'rest apis', 'restful api', 'restful apis',
    'restful web services', 'graphql', 'api-first', 'api first',
    # Architecture
    'micro services', 'microservices', 'event-driven', 'event driven',
    'distributed systems', 'service-oriented',
    # Containers & cloud
    'docker', 'kubernetes', 'k8s', 'aks', 'eks', 'ecs',
    'aws lambda', 'amazon web services',
    'google cloud', 'gcp', 'bigquery',
    # Testing
    'unit test', 'unit tests', 'unit testing',
    'integration test', 'integration tests', 'integration testing',
    'automated testing', 'test-driven', 'tdd', 'bdd',
    # Agile
    'agile', 'scrum', 'kanban',
    # Auth
    'oauth2', 'jwt', 'rbac', 'sso',
    # Messaging / caching
    'redis', 'kafka', 'rabbitmq', 'elasticsearch', 'sidekiq',
    # Infrastructure
    'terraform', 'ansible', 'jenkins',
    # Languages
    'python', 'golang', 'java', 'c#', 'c++', 'ruby', 'rust', 'swift',
    'typescript', 'javascript', 'kotlin', 'scala', 'php', 'perl',
    # Roles / paradigms
    'full stack', 'full-stack', 'backend', 'frontend', 'front end',
    'devops', 'sre', 'cloud native', 'cloud-native',
    # Data
    'data engineering', 'data pipeline', 'etl', 'data warehouse',
    # Compliance
    'hipaa', 'sox', 'pci', 'gdpr',
    # Financial / domain
    'netsuite', 'ach', 'payment processing',
    # Monitoring
    'new relic', 'datadog',
    # ORM / DB
    'postgresql', 'mysql', 'activerecord orm',
    # Other
    'pl/sql', 'peoplesoft', 'workday', 'salesforce', 'ca plex', 'oracle',
    'servicenow', 'service now',
    # Workday / ServiceNow platform specifics
    'workday studio', 'peci', 'picof', 'core connectors', 'eib', 'xslt',
    # Data platforms / warehouses / lakehouse
    'snowflake', 'databricks', 'redshift', 'delta lake', 'lakehouse',
    'data lake', 'data mesh', 'parquet', 'clickhouse', 'cassandra', 'neo4j',
    # Streaming / orchestration
    'kinesis', 'apache spark', 'spark', 'airflow', 'apache airflow',
    'dagster', 'temporal', 'prefect', 'mlflow',
    # AI tooling / assistants / patterns
    'openai', 'anthropic', 'claude', 'chatgpt', 'github copilot', 'copilot',
    'ai', 'ai/ml', 'genai', 'gen ai', 'llm', 'llms', 'mlops', 'ml ops', 'ml-ops',
    'ai model optimization', 'model optimization', 'fine-tuning', 'fine tuning',
    'rag', 'langchain', 'llamaindex', 'pinecone',
    'vector database', 'vector databases', 'vector db',
    'sagemaker', 'amazon sagemaker', 'bedrock', 'amazon bedrock',
    # IaC extensions
    'cloudformation', 'aws cloudformation', 'bicep', 'pulumi',
    'argocd', 'argo cd', 'istio',
    # Testing frameworks
    'cypress', 'playwright', 'selenium', 'jest', 'vitest',
    # UI libraries / state / styling
    'redux', 'storybook', 'tailwind', 'tailwind css', 'material ui',
    'react query', 'tanstack', 'mobx', 'd3.js',
    # Backend JS frameworks
    'nestjs', 'fastify', 'hono',
    # API / arch patterns
    'websocket', 'web socket', 'webrtc', 'soap',
    'domain-driven', 'domain driven', 'ddd',
    'clean architecture', 'hexagonal', 'observability', 'dependency injection',
    'role-based access control', 'continuous integration', 'continuous deployment',
    # Monitoring / observability
    'prometheus', 'grafana', 'opentelemetry', 'jaeger', 'loki',
    # Healthcare / compliance
    'fhir', 'hl7', 'dicom',
]

# Aliases so both forms of a name get the same weight
TECH_ALIASES = {
    'go': ['golang'],
    'golang': ['go'],
}

# Terms near these modifiers in the JD get a weight boost (signals must-have)
STRONG_MODIFIERS = [
    'expert', 'expertise', 'expert-level', 'command',
    'proficient', 'proficiency', 'deep', 'extensive',
    'advanced', 'mastery', 'strong background',
]

MUST_HAVE_MODIFIERS = [
    'must have', 'must-have',
]

# Known programming languages — missing a required language is a strong negative signal
PROGRAMMING_LANGUAGES = {
    'go', 'golang', 'python', 'typescript', 'javascript', 'java',
    'c#', 'c++', 'ruby', 'rust', 'swift', 'kotlin', 'scala', 'php', 'perl',
}

# ── Category sets for weighted scoring ──
FRAMEWORKS = {
    'spring boot', 'react', 'django', 'fastapi', 'fast api', '.net', '.net core',
    'asp.net', 'asp.net core', 'asp.net mvc', 'asp.net web api',
    'rails', 'ruby on rails', 'nestjs', 'express', 'express.js', 'next.js',
    'angular', 'flask', 'laravel', 'entity framework', 'entity framework core',
    'vue.js', 'svelte', 'htmx', 'spring cloud', 'quarkus', 'blazor',
    'react native', 'nuxt.js', 'react.js', 'node.js',
    'fastify', 'hono', 'redux', 'storybook', 'tailwind', 'tailwind css',
    'material ui', 'react query', 'tanstack', 'mobx',
}

ML_AI_DATA = {
    'machine learning', 'deep learning', 'natural language processing', 'nlp',
    'large language model', 'llm', 'llms', 'computer vision', 'generative ai',
    'agentic ai', 'ai', 'ai/ml', 'genai', 'gen ai',
    'mlops', 'ml ops', 'ml-ops',
    'ai model optimization', 'model optimization', 'fine-tuning', 'fine tuning',
    'etl', 'data pipeline', 'data warehouse', 'data engineering',
    'vertex ai',
    'databricks', 'snowflake', 'redshift', 'delta lake', 'lakehouse',
    'data lake', 'data mesh', 'parquet',
    'sagemaker', 'amazon sagemaker', 'bedrock', 'amazon bedrock', 'mlflow',
    'openai', 'anthropic', 'claude', 'chatgpt', 'github copilot', 'copilot',
    'rag', 'langchain', 'llamaindex', 'pinecone',
    'vector database', 'vector databases', 'vector db',
    'apache spark', 'spark', 'airflow', 'apache airflow',
    'dagster', 'temporal', 'prefect',
}

CLOUD_PLATFORMS = {
    'aws', 'azure', 'gcp', 'google cloud', 'amazon web services',
}

DATABASES_DATASTORES = {
    'postgresql', 'mysql', 'sql server', 'mongodb', 'dynamodb',
    'cosmos db', 'redis', 'elasticsearch', 'bigquery', 'oracle',
    'cassandra', 'neo4j', 'clickhouse', 'snowflake', 'redshift',
}

DEVOPS_CONTAINERS = {
    'docker', 'kubernetes', 'k8s', 'terraform', 'ansible', 'jenkins',
    'github actions', 'azure pipelines', 'azure devops', 'ci/cd',
    'eks', 'ecs', 'aks', 'helm',
    'cloudformation', 'aws cloudformation', 'bicep', 'pulumi',
    'argocd', 'argo cd', 'istio',
    'continuous integration', 'continuous deployment',
}

ARCHITECTURE_API = {
    'microservices', 'micro services', 'rest api', 'rest apis',
    'restful api', 'restful apis', 'restful web services',
    'graphql', 'grpc', 'event-driven', 'event driven',
    'distributed systems', 'service-oriented', 'api-first', 'api first',
    'soap', 'websocket', 'web socket', 'webrtc',
    'domain-driven', 'domain driven', 'ddd',
    'clean architecture', 'hexagonal', 'dependency injection',
}

MESSAGING_STREAMING = {
    'kafka', 'rabbitmq', 'sqs', 'azure service bus', 'sidekiq',
    'activemq', 'redis pub/sub', 'kinesis',
}

SECURITY_AUTH = {
    'oauth2', 'jwt', 'rbac', 'sso', 'hipaa', 'sox', 'pci', 'gdpr',
    'role-based access control', 'fhir', 'hl7', 'dicom',
}

MONITORING_OBSERVABILITY = {
    'datadog', 'new relic', 'application insights', 'prometheus',
    'grafana', 'elk stack', 'splunk',
    'opentelemetry', 'jaeger', 'loki', 'observability',
}

DOMAIN_PLATFORMS = {
    'dynamics 365', 'dynamics crm', 'salesforce', 'netsuite',
    'power platform', 'power bi', 'power apps', 'power automate',
    'workday', 'peoplesoft', 'sap', 'servicenow', 'service now',
    'workday studio', 'peci', 'picof', 'core connectors', 'eib', 'xslt',
}

METHODOLOGY_GENERIC = {
    'agile', 'scrum', 'kanban', 'tdd', 'bdd',
    'unit test', 'unit tests', 'unit testing',
    'integration test', 'integration tests', 'integration testing',
    'automated testing', 'test-driven',
    'code review', 'pair programming',
    'cypress', 'playwright', 'selenium', 'jest', 'vitest',
}

# Build a single lookup: term → multiplier
CATEGORY_MULTIPLIERS = {}
for _terms, _mult in [
    (PROGRAMMING_LANGUAGES, 3.0),
    (ML_AI_DATA, 2.8),
    (FRAMEWORKS, 2.5),
    (CLOUD_PLATFORMS, 2.0),
    (DATABASES_DATASTORES, 1.8),
    (DEVOPS_CONTAINERS, 1.5),
    (ARCHITECTURE_API, 1.3),
    (MESSAGING_STREAMING, 1.3),
    (SECURITY_AUTH, 1.2),
    (MONITORING_OBSERVABILITY, 1.1),
    (DOMAIN_PLATFORMS, 1.2),
    (METHODOLOGY_GENERIC, 1.0),
]:
    for _t in _terms:
        CATEGORY_MULTIPLIERS.setdefault(_t, _mult)

# Words too generic to help discriminate between resumes
STOP_WORDS = {
    # Articles, pronouns, prepositions, etc.
    'the', 'and', 'for', 'are', 'you', 'our', 'with', 'that', 'this',
    'from', 'your', 'have', 'will', 'can', 'not', 'but', 'all', 'been',
    'into', 'who', 'what', 'how', 'its', 'also', 'just', 'let', 'don',
    'we', 'in', 'to', 'of', 'is', 'it', 'an', 'or', 'be', 'do', 'if',
    'as', 'at', 'by', 'on', 'so', 'up', 'no', 'us', 'am', 'he', 'she',
    'his', 'her', 'was', 'has', 'had', 'did', 'may', 'get', 'set',
    'about', 'along', 'based', 'ready', 'send', 'open', 'mode',
    'work', 'team', 'help', 'look', 'talk', 'dive', 'please',
    'details', 'status', 'current', 'expected', 'earliest',
    # Generic JD & resume filler
    'experience', 'using', 'including', 'such', 'like', 'need',
    'must', 'should', 'would', 'could', 'working', 'looking',
    'able', 'years', 'role', 'job', 'company', 'candidate',
    'strong', 'excellent', 'proven', 'ensure', 'develop',
    'maintain', 'support', 'provide', 'create', 'implement',
    'manage', 'design', 'build', 'deliver', 'drive', 'across',
    'cross-functional', 'collaborative', 'communication',
    'skills', 'knowledge', 'understanding', 'proficient',
    'requirements', 'responsible', 'responsibilities',
    'environment', 'systems', 'solutions', 'services',
    'application', 'applications', 'software', 'engineering',
    'development', 'technology', 'technologies', 'technical',
    'performance', 'quality', 'process', 'processes',
    'data', 'code', 'tools', 'platform', 'practices',
    'project', 'projects', 'business', 'clients',
    'well', 'high', 'new', 'key', 'within', 'other',
    'opportunity', 'offer', 'join', 'learn', 'stay',
    'including', 'various', 'multiple', 'related',
    # Role-level words (appear in almost every resume; not discriminative)
    'senior', 'junior', 'lead', 'principal', 'staff',
    'engineer', 'developer', 'architect', 'manager', 'consultant',
    'cloud',  # too generic alone; specific phrases like 'google cloud' still match
}


# ──────────────────────────────────────────────
# I/O helpers
# ──────────────────────────────────────────────
def read_job_description():
    with open(JOB_DESC_PATH, 'r', encoding='utf-8') as f:
        return f.read()


def read_docx_text(path):
    doc = Document(str(path))
    return '\n'.join(p.text for p in doc.paragraphs)


def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')


def get_resume_text(path, cache):
    key = path.name
    mtime = str(path.stat().st_mtime)
    if key in cache and cache[key].get('mtime') == mtime:
        return cache[key]['text']
    text = read_docx_text(path)
    cache[key] = {'mtime': mtime, 'text': text}
    return text


# ──────────────────────────────────────────────
# Text analysis helpers
# ──────────────────────────────────────────────
def extract_single_keywords(text):
    """Extract meaningful single-word tokens (excludes stop words)."""
    tokens = re.findall(r'[a-zA-Z#\+\.]{2,}', text.lower())
    return [t for t in tokens if t not in STOP_WORDS and len(t) > 2]


def _word_boundary_check(text_lower, phrase):
    """Whole-word / whole-phrase match using letter-boundaries."""
    pattern = r'(?<![a-zA-Z])' + re.escape(phrase) + r'(?![a-zA-Z])'
    return bool(re.search(pattern, text_lower))


def extract_phrases_from_text(text):
    """Find all known tech phrases present in the text."""
    text_lower = text.lower()
    return [ph for ph in TECH_PHRASES if _word_boundary_check(text_lower, ph)]


def extract_title(job_text):
    """Return the first non-empty, non-trivially-short line as the job title."""
    for line in job_text.split('\n'):
        line = line.strip()
        if line and len(line) < 200:
            return line
    return ''


def parse_jd_sections(job_text):
    """Split the JD into title / required / preferred / general buckets."""
    title = extract_title(job_text)
    lines = job_text.split('\n')
    required, preferred, general = [], [], []
    section = 'general'

    REQUIRED_CUES = [
        'required', 'must have', 'must-have', 'qualifications',
        'key responsibilities', 'responsibilities', 'experience:',
        'skills:', 'educational background',
        "you'll be doing", 'you will be doing',
        'what experience',
    ]
    PREFERRED_CUES = [
        'preferred', 'nice to have', 'nice-to-have', 'bonus',
        'plus', 'additional', 'technological proficiency',
    ]
    IGNORE_CUES = [
        'what we offer', 'benefits', 'equal opportunity',
        'e-verify', 'personal attributes', 'total rewards',
    ]

    for line in lines:
        low = line.strip().lower()
        if any(c in low for c in IGNORE_CUES):
            section = 'ignore'
        elif any(c in low for c in PREFERRED_CUES):
            section = 'preferred'
        elif any(c in low for c in REQUIRED_CUES):
            section = 'required'

        if section == 'required':
            required.append(line)
        elif section == 'preferred':
            preferred.append(line)
        elif section != 'ignore':
            general.append(line)

    return {
        'title': title,
        'required': '\n'.join(required),
        'preferred': '\n'.join(preferred),
        'general': '\n'.join(general),
    }


# ──────────────────────────────────────────────
# Profile building (section-aware weighting)
# ──────────────────────────────────────────────
def build_job_profile(job_text):
    """Build a weighted term→importance dict from the JD.

    Weight tiers (cumulative when a term appears in multiple sections):
      Title terms          : +25 (phrase)  / +20 (keyword)
      Required section     : +8  (phrase)  / 3–5 (keyword, freq-scaled)
            Must-have terms      : +10 extra
      Preferred section    : +5  (phrase)  / 2–3 (keyword)
      General section      : +4  (phrase)  / 2–3 (keyword)
    """
    sections = parse_jd_sections(job_text)
    profile = {}

    # ── Title (highest priority — defines the role) ──
    title = sections['title']
    for ph in extract_phrases_from_text(title):
        profile[ph] = profile.get(ph, 0) + 25
    for kw in extract_single_keywords(title):
        profile[kw] = profile.get(kw, 0) + 20

    # ── Required / Qualifications ──
    req = sections['required']
    if req:
        for ph in extract_phrases_from_text(req):
            profile[ph] = profile.get(ph, 0) + 8
        freq = {}
        for kw in extract_single_keywords(req):
            freq[kw] = freq.get(kw, 0) + 1
        for kw, cnt in freq.items():
            profile[kw] = profile.get(kw, 0) + min(cnt, 3) + 2

    # ── Preferred / Nice-to-have ──
    pref = sections['preferred']
    if pref:
        for ph in extract_phrases_from_text(pref):
            profile[ph] = profile.get(ph, 0) + 5
        freq = {}
        for kw in extract_single_keywords(pref):
            freq[kw] = freq.get(kw, 0) + 1
        for kw, cnt in freq.items():
            profile[kw] = profile.get(kw, 0) + min(cnt, 2) + 1

    # ── General / intro ──
    gen = sections['general']
    if gen:
        for ph in extract_phrases_from_text(gen):
            profile[ph] = profile.get(ph, 0) + 4
        freq = {}
        for kw in extract_single_keywords(gen):
            freq[kw] = freq.get(kw, 0) + 1
        for kw, cnt in freq.items():
            profile[kw] = profile.get(kw, 0) + min(cnt, 2) + 1

    # ── Modifier boost — terms near "expert", "strong", etc. get 2× weight ──
    req_text = sections.get('required', '')
    if req_text:
        for sentence in re.split(r'[.\n]', req_text):
            sent_lower = sentence.lower()
            if any(mod in sent_lower for mod in MUST_HAVE_MODIFIERS):
                for ph in extract_phrases_from_text(sentence):
                    profile[ph] = profile.get(ph, 0) + 10
                for kw in extract_single_keywords(sentence):
                    profile[kw] = profile.get(kw, 0) + 10
            if any(mod in sent_lower for mod in STRONG_MODIFIERS):
                for ph in extract_phrases_from_text(sentence):
                    if ph in profile:
                        profile[ph] = int(profile[ph] * 2)

    # Propagate aliases so both forms get equal weight
    alias_additions = {}
    for term, weight in profile.items():
        if term in TECH_ALIASES:
            for alias in TECH_ALIASES[term]:
                alias_additions[alias] = max(alias_additions.get(alias, 0), weight)
    for alias, weight in alias_additions.items():
        profile[alias] = max(profile.get(alias, 0), weight)

    return profile


# ──────────────────────────────────────────────
# IDF — terms appearing in many resumes are less discriminative
# ──────────────────────────────────────────────
def compute_idf(all_texts, terms):
    n = len(all_texts)
    if n == 0:
        return {t: 1.0 for t in terms}
    doc_freq = {t: 0 for t in terms}
    for text in all_texts:
        text_lower = text.lower()
        for t in terms:
            if t in text_lower:
                doc_freq[t] += 1
    idf = {}
    for t in terms:
        df = doc_freq[t]
        idf[t] = math.log(n / df) + 1.0 if df > 0 else 1.5
    return idf


# ──────────────────────────────────────────────
# Scoring
# ──────────────────────────────────────────────
def score_resume(resume_text, job_profile, filename, idf, critical_languages=None):
    resume_lower = resume_text.lower()
    score = 0.0

    # ── Content matching (importance × log-capped frequency × IDF × category multiplier) ──
    for term, weight in job_profile.items():
        pattern = r'(?<![a-zA-Z])' + re.escape(term) + r'(?![a-zA-Z])'
        count = len(re.findall(pattern, resume_lower))
        if count > 0:
            effective = 1 + math.log(min(count, 10))
            cat_mult = CATEGORY_MULTIPLIERS.get(term, 1.0)
            score += weight * effective * idf.get(term, 1.0) * cat_mult

    # ── Filename tag matching (strong curated signal) ──
    fname_match = re.search(r'\(([^)]+)\)', filename)
    if fname_match:
        tag_str = fname_match.group(1).lower()
        tags = [t.strip() for t in tag_str.split('+') if t.strip()]
        for tag in tags:
            for term, weight in job_profile.items():
                if tag == term or tag in term or term in tag:
                    score += weight * 2.0
                    break

    # ── Critical language penalty ──
    # If the JD requires expert-level programming languages, penalise resumes
    # that don't mention them at all (language mismatch is a deal-breaker).
    if critical_languages:
        for lang in critical_languages:
            # Check the language and all its aliases
            lang_terms = [lang] + TECH_ALIASES.get(lang, [])
            found = False
            for lt in lang_terms:
                if len(lt) <= 3:
                    if re.search(r'(?<![a-zA-Z])' + re.escape(lt) + r'(?![a-zA-Z])', resume_lower):
                        found = True
                        break
                else:
                    if lt in resume_lower:
                        found = True
                        break
            if not found:
                score *= 0.7  # 30 % penalty per missing critical language

    return round(score)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def find_best_resume():
    job_text = read_job_description()
    job_profile = build_job_profile(job_text)

    # Detect critical programming languages (mentioned near strong modifiers)
    sections = parse_jd_sections(job_text)
    critical_languages = set()
    req_text = sections.get('required', '')
    if req_text:
        for sentence in re.split(r'[.\n]', req_text):
            sent_lower = sentence.lower()
            if any(mod in sent_lower for mod in STRONG_MODIFIERS):
                for ph in extract_phrases_from_text(sentence):
                    if ph in PROGRAMMING_LANGUAGES:
                        critical_languages.add(ph)
    # Collapse aliases so we only check once per concept
    collapsed = set()
    seen = set()
    for lang in critical_languages:
        canonical = min([lang] + TECH_ALIASES.get(lang, []))
        if canonical not in seen:
            collapsed.add(canonical)
            seen.add(canonical)
            for alias in TECH_ALIASES.get(lang, []):
                seen.add(alias)
    critical_languages = collapsed

    print("Job profile terms (top 20 by weight):")
    sorted_terms = sorted(job_profile.items(), key=lambda x: x[1], reverse=True)
    for term, weight in sorted_terms[:20]:
        print(f"  {weight:>4}  {term}")
    if critical_languages:
        print(f"\nCritical languages: {', '.join(sorted(critical_languages))}")
    print()

    resumes = list(RESUMES_DIR.glob("*.docx"))
    if not resumes:
        print("No .docx resumes found in /resumes folder.")
        return None

    cache = load_cache()
    resume_data = []
    cache_hits = 0
    for rpath in resumes:
        try:
            cached = rpath.name in cache and cache[rpath.name].get('mtime') == str(rpath.stat().st_mtime)
            text = get_resume_text(rpath, cache)
            if cached:
                cache_hits += 1
            resume_data.append((rpath, text))
        except Exception as e:
            print(f"  ERROR  {rpath.name}: {e}")
    save_cache(cache)

    # Compute IDF across all resumes
    all_texts = [t for _, t in resume_data]
    idf = compute_idf(all_texts, job_profile.keys())

    # Score & rank
    results = []
    for rpath, text in resume_data:
        sc = score_resume(text, job_profile, rpath.name, idf, critical_languages)
        results.append((sc, rpath.name, rpath))
        print(f"  {sc:>6}  {rpath.name}")

    print(f"\n({cache_hits}/{len(resumes)} resumes loaded from cache)")

    results.sort(key=lambda x: x[0], reverse=True)
    best_score, best_name, best_path = results[0]

    print(f"\n>>> Best match: {best_name} (score: {best_score})")

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(best_name)

    return best_path


if __name__ == "__main__":
    find_best_resume()
