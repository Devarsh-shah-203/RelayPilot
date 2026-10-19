"""DOM selector constants used by the browser-chat components.

Selectors are deliberately structural and provider-neutral where possible.
Provider-specific upload controls belong in provider adapters, not here.
"""

_COMPOSER_SELECTOR = (
    "textarea:visible, "
    "[contenteditable='true']:visible, "
    "[role='textbox']:visible, "
    "input:not([type]):visible, "
    "input[type='text']:visible, "
    "input[type='search']:visible"
)

_BUTTON_SELECTOR = (
    "button:visible, [role='button']:visible, input[type='submit']:visible"
)

_ASSISTANT_SELECTORS = (
    '[data-message-author-role="assistant"], '
    '[data-role="assistant"], '
    '[data-author="assistant"], '
    '[data-message-role="assistant"], '
    '[data-testid*="assistant-message" i], '
    '[data-testid*="model-response" i], '
    '[class*="assistant-message" i], '
    '[class*="message-assistant" i], '
    'model-response'
)

_USER_SELECTORS = (
    '[data-message-author-role="user"], '
    '[data-role="user"], '
    '[data-author="user"], '
    '[data-message-role="user"]'
)

_GENERIC_MESSAGE_SELECTORS = (
    '[data-testid*="conversation-turn" i]',
    '[data-testid*="message" i]',
    '[data-testid*="response" i]',
    'model-response',
    'article',
    '[role="article"]',
    '[role="log"] > *',
    '[role="feed"] > *',
    '[class*="message-content" i]',
    '[class*="markdown" i]',
    '[class*="prose" i]',
    '[class*="assistant" i]',
    '[class*="answer" i]',
    '[class*="response" i]'
)

_DIALOG_SELECTOR = (
    '[role="alertdialog"]:visible, '
    '[role="dialog"]:visible, '
    '[aria-modal="true"]:visible'
)
