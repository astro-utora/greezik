"""Match the best-fitting resume from ``APPLICANT_RESUMES_DIR`` to a JD.

Ported from ``dice_auto_bidder/steps/step1_match_resume.py`` and adapted
to work with PDF resumes and Greezik's :class:`ResumeIndex`. The
scoring strategy is identical to the original:

* Title terms get the highest weight.
* JD sections (required / preferred / general) are weighted differently.
* IDF down-weights terms that appear in every resume.
* Filename tags in parentheses (e.g. ``(.NET, Angular, Azure)``) are a
  strong signal proportional to JD importance.
* Critical programming-language mismatches incur a 30 % penalty.

The public entrypoint :func:`match_best_resume` accepts JD text +
:class:`ResumeIndex` and returns ``(best_path, best_score, ranking)``.
"""

from __future__ import annotations

import logging
import math
import re

from .resume_index import ResumeEntry, ResumeIndex

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary -- copied verbatim from step1_match_resume.py
# ---------------------------------------------------------------------------


TECH_PHRASES: list[str] = [
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


TECH_ALIASES: dict[str, list[str]] = {
    'go': ['golang'],
    'golang': ['go'],
}


STRONG_MODIFIERS = [
    'expert', 'expertise', 'expert-level', 'command',
    'proficient', 'proficiency', 'deep', 'extensive',
    'advanced', 'mastery', 'strong background',
]

MUST_HAVE_MODIFIERS = [
    'must have', 'must-have',
]

PROGRAMMING_LANGUAGES = {
    'go', 'golang', 'python', 'typescript', 'javascript', 'java',
    'c#', 'c++', 'ruby', 'rust', 'swift', 'kotlin', 'scala', 'php', 'perl',
}

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


CATEGORY_MULTIPLIERS: dict[str, float] = {}
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


STOP_WORDS = {
    'the', 'and', 'for', 'are', 'you', 'our', 'with', 'that', 'this',
    'from', 'your', 'have', 'will', 'can', 'not', 'but', 'all', 'been',
    'into', 'who', 'what', 'how', 'its', 'also', 'just', 'let', 'don',
    'we', 'in', 'to', 'of', 'is', 'it', 'an', 'or', 'be', 'do', 'if',
    'as', 'at', 'by', 'on', 'so', 'up', 'no', 'us', 'am', 'he', 'she',
    'his', 'her', 'was', 'has', 'had', 'did', 'may', 'get', 'set',
    'about', 'along', 'based', 'ready', 'send', 'open', 'mode',
    'work', 'team', 'help', 'look', 'talk', 'dive', 'please',
    'details', 'status', 'current', 'expected', 'earliest',
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
    'senior', 'junior', 'lead', 'principal', 'staff',
    'engineer', 'developer', 'architect', 'manager', 'consultant',
    'cloud',
}


# ---------------------------------------------------------------------------
# Text analysis
# ---------------------------------------------------------------------------


def extract_single_keywords(text: str) -> list[str]:
    tokens = re.findall(r'[a-zA-Z#\+\.]{2,}', text.lower())
    return [t for t in tokens if t not in STOP_WORDS and len(t) > 2]


def _word_boundary_check(text_lower: str, phrase: str) -> bool:
    pattern = r'(?<![a-zA-Z])' + re.escape(phrase) + r'(?![a-zA-Z])'
    return bool(re.search(pattern, text_lower))


def extract_phrases_from_text(text: str) -> list[str]:
    text_lower = text.lower()
    return [ph for ph in TECH_PHRASES if _word_boundary_check(text_lower, ph)]


def extract_title(job_text: str) -> str:
    for line in job_text.split('\n'):
        line = line.strip()
        if line and len(line) < 200:
            return line
    return ''


def parse_jd_sections(job_text: str) -> dict[str, str]:
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


# ---------------------------------------------------------------------------
# Profile + IDF + score (verbatim from step1_match_resume.py)
# ---------------------------------------------------------------------------


def build_job_profile(job_text: str) -> dict[str, int]:
    sections = parse_jd_sections(job_text)
    profile: dict[str, int] = {}

    title = sections['title']
    for ph in extract_phrases_from_text(title):
        profile[ph] = profile.get(ph, 0) + 25
    for kw in extract_single_keywords(title):
        profile[kw] = profile.get(kw, 0) + 20

    req = sections['required']
    if req:
        for ph in extract_phrases_from_text(req):
            profile[ph] = profile.get(ph, 0) + 8
        freq: dict[str, int] = {}
        for kw in extract_single_keywords(req):
            freq[kw] = freq.get(kw, 0) + 1
        for kw, cnt in freq.items():
            profile[kw] = profile.get(kw, 0) + min(cnt, 3) + 2

    pref = sections['preferred']
    if pref:
        for ph in extract_phrases_from_text(pref):
            profile[ph] = profile.get(ph, 0) + 5
        freq = {}
        for kw in extract_single_keywords(pref):
            freq[kw] = freq.get(kw, 0) + 1
        for kw, cnt in freq.items():
            profile[kw] = profile.get(kw, 0) + min(cnt, 2) + 1

    gen = sections['general']
    if gen:
        for ph in extract_phrases_from_text(gen):
            profile[ph] = profile.get(ph, 0) + 4
        freq = {}
        for kw in extract_single_keywords(gen):
            freq[kw] = freq.get(kw, 0) + 1
        for kw, cnt in freq.items():
            profile[kw] = profile.get(kw, 0) + min(cnt, 2) + 1

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

    alias_additions: dict[str, int] = {}
    for term, weight in profile.items():
        if term in TECH_ALIASES:
            for alias in TECH_ALIASES[term]:
                alias_additions[alias] = max(alias_additions.get(alias, 0), weight)
    for alias, weight in alias_additions.items():
        profile[alias] = max(profile.get(alias, 0), weight)

    return profile


def compute_idf(all_texts: list[str], terms) -> dict[str, float]:
    n = len(all_texts)
    if n == 0:
        return {t: 1.0 for t in terms}
    doc_freq = {t: 0 for t in terms}
    for text in all_texts:
        text_lower = text.lower()
        for t in terms:
            if t in text_lower:
                doc_freq[t] += 1
    idf: dict[str, float] = {}
    for t in terms:
        df = doc_freq[t]
        idf[t] = math.log(n / df) + 1.0 if df > 0 else 1.5
    return idf


def score_resume(
    resume_text: str,
    job_profile: dict[str, int],
    filename: str,
    idf: dict[str, float],
    critical_languages: set[str] | None = None,
) -> int:
    resume_lower = resume_text.lower()
    score = 0.0

    for term, weight in job_profile.items():
        pattern = r'(?<![a-zA-Z])' + re.escape(term) + r'(?![a-zA-Z])'
        count = len(re.findall(pattern, resume_lower))
        if count > 0:
            effective = 1 + math.log(min(count, 10))
            cat_mult = CATEGORY_MULTIPLIERS.get(term, 1.0)
            score += weight * effective * idf.get(term, 1.0) * cat_mult

    fname_match = re.search(r'\(([^)]+)\)', filename)
    if fname_match:
        tag_str = fname_match.group(1).lower()
        tags = [t.strip() for t in tag_str.split(',') if t.strip()]
        # The original step1 split on '+'; the actual filenames in
        # Tyler's PDF folder use commas (".NET, Angular, Azure"). Split
        # on both so either convention works.
        if len(tags) == 1:
            tags = [t.strip() for t in tag_str.split('+') if t.strip()]
        for tag in tags:
            for term, weight in job_profile.items():
                if tag == term or tag in term or term in tag:
                    score += weight * 2.0
                    break

    if critical_languages:
        for lang in critical_languages:
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
                score *= 0.7

    return round(score)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def detect_critical_languages(job_text: str) -> set[str]:
    """Languages flagged as expert-level required in the JD's must-have section."""
    sections = parse_jd_sections(job_text)
    critical: set[str] = set()
    req_text = sections.get('required', '')
    if not req_text:
        return critical
    for sentence in re.split(r'[.\n]', req_text):
        sent_lower = sentence.lower()
        if any(mod in sent_lower for mod in STRONG_MODIFIERS):
            for ph in extract_phrases_from_text(sentence):
                if ph in PROGRAMMING_LANGUAGES:
                    critical.add(ph)
    collapsed: set[str] = set()
    seen: set[str] = set()
    for lang in critical:
        canonical = min([lang] + TECH_ALIASES.get(lang, []))
        if canonical not in seen:
            collapsed.add(canonical)
            seen.add(canonical)
            for alias in TECH_ALIASES.get(lang, []):
                seen.add(alias)
    return collapsed


def match_best_resume(
    job_text: str,
    index: ResumeIndex,
) -> tuple[ResumeEntry | None, int, list[tuple[int, ResumeEntry]]]:
    """Return ``(best_entry, best_score, ranked)`` for ``job_text``.

    ``ranked`` is the full sorted list of ``(score, entry)``. If the
    index is empty, returns ``(None, 0, [])``.
    """

    if not index.entries:
        return None, 0, []

    job_profile = build_job_profile(job_text)
    critical = detect_critical_languages(job_text)

    all_texts = [e.text for e in index.entries if e.text]
    idf = compute_idf(all_texts, list(job_profile.keys()))

    scored: list[tuple[int, ResumeEntry]] = []
    for entry in index.entries:
        if not entry.text:
            scored.append((0, entry))
            continue
        sc = score_resume(entry.text, job_profile, entry.name, idf, critical)
        scored.append((sc, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_entry = scored[0]
    return best_entry, best_score, scored
