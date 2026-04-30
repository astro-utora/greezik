"""Centralized selectors for jobright.ai.

Ant Design generates css-* class hashes that rotate between builds, so we avoid
those and rely on:

- stable element ids (e.g. ``#basic_email``)
- the project-specific ``index_*__hash`` class prefix (the prefix is part of the
  source code; only the hash suffix changes)
- visible text via ``:has-text(...)``
"""

from __future__ import annotations

# URLs ---------------------------------------------------------------------

HOMEPAGE_URL = "https://jobright.ai/"
RECOMMEND_URL = "https://jobright.ai/jobs/recommend"
RECOMMEND_PATH = "/jobs/recommend"
APPLIED_URL = "https://jobright.ai/jobs/applied"
APPLIED_PATH = "/jobs/applied"

# Auth ---------------------------------------------------------------------

# The header SIGN IN entry is rendered as a <span> with exact text "SIGN IN".
# We deliberately use Playwright's exact-text matcher in code (auth.py) rather
# than a CSS :has-text selector, because :has-text is a case-insensitive
# substring match on descendant text and would also match the footer's
# "sign In" link or any wrapper element containing the substring.
SIGN_IN_TEXT = "SIGN IN"
EMAIL_INPUT = "#basic_email"
PASSWORD_INPUT = "#basic_password"
# The submit button inside the sign-in modal also contains a span with text
# "SIGN IN", but it's the only [type=submit] button on the page while the
# modal is open, so this remains unambiguous.
SIGN_IN_SUBMIT = 'button[type="submit"]:has-text("SIGN IN")'

# Job feed -----------------------------------------------------------------

# Job-list cards expose the role title as an ``<h2>`` with the
# ``index_job-title__`` class hash. Clicking the title opens the
# detail panel (URL becomes ``/jobs/info/<id>``).
JOB_TITLE_LIST = 'h2[class*="index_job-title__"]:visible'

# Apply buttons surface in two places (job list cards + the detail
# panel) and -- as of jobright's late-2026 redesign -- share both a
# stable ``id="apply-now-button-id"`` and a camelCase class
# ``index_applyButton__<hash>``. We previously keyed off the older
# kebab-case ``index_apply-button__`` which no longer matches anything,
# so we accept ALL three so the live runner keeps working across builds.
# The visible labels are either ``Apply with Autofill`` or ``APPLY NOW``.
APPLY_BUTTONS = (
    'button#apply-now-button-id:visible, '
    'button[class*="index_applyButton__"]:visible, '
    'button[class*="index_apply-button__"]:visible'
)

# Modal that may appear instead of opening a new tab directly.
APPLY_WITHOUT_CUSTOMIZING = (
    'button[class*="index_cancelButton__"]:has-text("Apply without Customizing")'
)

# Job detail panel ---------------------------------------------------------
#
# The detail panel is the overlay/route that opens after clicking a job
# title. It shows the full role write-up and a single Apply button. We
# scrape every "Job_Description.txt" field off this DOM rather than from
# the external Greenhouse popup.

# Hash differs from the job-list ``<h2>`` so we anchor on the source
# class prefix.
JOB_INFO_TITLE = 'h1[class*="index_job-title__"]'
JOB_INFO_COMPANY_NAME = 'span[class*="index_company-name__"]'
JOB_INFO_COMPANY_SUMMARY = 'div[class*="index_company-summary__"]'

# A row on the metadata strip (location, employment type, salary, ...).
# We iterate these and pick out the one with a ``money`` icon.
JOB_METADATA_ITEM = 'div[class*="index_job-metadata-item__"]'

# Each <section> below the metadata strip is one of: Responsibilities,
# Qualification, Benefits, etc. Identified by the ``<h2>`` label child.
JOB_INFO_SECTION = 'section[class*="index_sectionContent__"]'
JOB_INFO_SECTION_LABEL = 'h2[class*="index_label__"]'
JOB_INFO_LIST_TEXT = 'span[class*="index_listText__"]'
JOB_INFO_QUALIFICATION_TAG = 'span[class*="index_qualification-tag__"]'

# X / close button for the detail panel.
JOB_DETAIL_CLOSE_BUTTON = 'button[id*="index_job-detail-close-button__"]'

# Confirmation popup that appears after an external apply.
# The "Did you apply?" modal contains a green primary button labelled
# "Yes, I applied!". Earlier code keyed off ``index_job-apply-confirm-popup-yes-button__``
# but the class hash / structure has rotated, so we use text + role instead.
YES_I_APPLIED_TEXT = "Yes, I applied!"
DID_YOU_APPLY_HEADING = "Did you apply"

# Unwanted modal handling -------------------------------------------------
#
# Jobright sometimes pops up onboarding / promotional / upsell modals on the
# recommend page that intercept clicks. We dismiss them by clicking their X
# close button, but we must NOT dismiss the apply-customization modal or the
# Yes-I-applied confirmation -- so we identify "good" modals by visible text
# and skip them.

OVERLAY_CONTAINERS = (
    ".ant-modal:visible, "
    ".ant-modal-content:visible, "
    ".ant-drawer-content:visible, "
    "[role='dialog']:visible"
)

GOOD_MODAL_TEXT_MARKERS = (
    "Apply without Customizing",
    YES_I_APPLIED_TEXT,
    DID_YOU_APPLY_HEADING,
)

# Selectors for X / close buttons inside an overlay container.
MODAL_CLOSE_SELECTORS = (
    "button.ant-modal-close, "
    ".ant-modal-close, "
    ".ant-drawer-close, "
    "button[aria-label='Close'], "
    "button[aria-label='close'], "
    "button:has(.anticon-close), "
    "[class*='close-x']"
)

# Applied-jobs page (/jobs/applied) ----------------------------------------
#
# Each card on this page surfaces a role title (``<h2>``), the company
# name (``<div>``), an "X hours/days/weeks/months ago" publish-time
# tag, and an X / remove-card button. We rely on the ``index_*__hash``
# class prefix because the trailing hash rotates between deploys.

APPLIED_JOB_TITLE = 'h2[class*="index_job-title__"]:visible'
# On /jobs/applied the company name renders as a <div>, not the <span>
# the recommend page uses. Match both so a future redesign of either
# page doesn't break this selector.
APPLIED_COMPANY_NAME = (
    'div[class*="index_company-name__"]:visible, '
    'span[class*="index_company-name__"]:visible'
)
APPLIED_PUBLISH_TIME = 'span[class*="index_publish-time__"]:visible'
APPLIED_REMOVE_BUTTON = 'button[id*="index_remove-card-button-id__"]:visible'
