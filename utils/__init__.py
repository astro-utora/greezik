"""Compatibility shim package used by the vendored ``steps/`` files.

Lives at the project root (``<root>/utils/``) so
``steps/step1_match_resume.py`` can do ``from utils.paths import
resumes_dir`` after adding its ``BASE_DIR`` (= the project root) to
``sys.path``. Greezik's own modules go through
:mod:`greezik.resume_index` instead and don't import from here.
"""
