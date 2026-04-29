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

# ``index_apply-button__`` is part of the source class name. We accept either
# ``Apply with Autofill`` or ``APPLY NOW`` as visible labels.
APPLY_BUTTONS = (
    'button[class*="index_apply-button__"]:visible'
)

# Modal that may appear instead of opening a new tab directly.
APPLY_WITHOUT_CUSTOMIZING = (
    'button[class*="index_cancelButton__"]:has-text("Apply without Customizing")'
)

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
