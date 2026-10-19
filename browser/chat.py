"""Provider-neutral browser chat transport built on Playwright.

This module is the stable facade used by the rest of AI Gateway. Browser-page
discovery, composer interaction, response extraction, page-state detection, and
submission verification live in ``browser.chat_utils``. Keeping the public
``BrowserChat`` API here minimizes changes to callers and isolates responsibilities.

No file-attachment behavior is implemented in this refactor.
"""

from __future__ import annotations
from typing import List, Optional, Tuple
from playwright.sync_api import Browser, Locator, Page
from browser.chat_utils.selectors import _COMPOSER_SELECTOR
import time


from browser.chat_utils import (
    ComposerMixin,
    PageStateMixin,
    ResponseExtractionMixin,
    SubmissionVerificationMixin,
)


class BrowserChat(
    ComposerMixin,
    ResponseExtractionMixin,
    PageStateMixin,
    SubmissionVerificationMixin,
):
    """Interact with a browser-based chat interface without provider labels.

    The existing method names and return-state contract are preserved. Helper
    behavior is provided by focused mixins in ``browser.chat_utils``.
    """

    def __init__(
        self,
        browser: Browser,
        page: Optional[Page] = None,
        *,
        composer_timeout: float = 15.0,
        insertion_timeout: float = 30.0,
        send_timeout: float = 30.0,
        submission_timeout: float = 12.0,
        response_timeout: float = 180.0,
        image_generation_timeout: float = 600.0,
        response_stability: float = 2.5,
        poll_interval: float = 0.25,
    ) -> None:
        self.browser = browser
        self.composer_timeout = composer_timeout
        self.insertion_timeout = insertion_timeout
        self.send_timeout = send_timeout
        self.submission_timeout = submission_timeout
        self.response_timeout = response_timeout
        # Image generation can take substantially longer than ordinary text
        # responses. This timeout is used only when the outgoing prompt appears
        # to explicitly request generated visual content.
        self.image_generation_timeout = image_generation_timeout
        self.response_stability = response_stability
        self.poll_interval = poll_interval
        self._active_prompt: Optional[str] = None
        self._active_image_generation = False
        self._last_prompt_verification: dict[str, object] = {}

        self.page = page or self._find_chat_page()
        if self.page is None:
            raise RuntimeError(
                "No browser page is available. Open the target chat page and try again."
            )


    def wait_for_response(
        self,
        previous_text: str,
        previous_assistant_count: Optional[int] = None,
        previous_generic_count: Optional[int] = None,
        previous_media_signature: Tuple[str, ...] = (),
    ) -> Tuple[str, str]:
        """Wait for a new assistant response and return its readable text.

        Explicit image-generation requests receive a longer timeout. The waiter
        also detects an image-only assistant turn, reports it in the terminal,
        and returns any accompanying assistant text for the CLI to display.
        """
        start = time.monotonic()
        timeout = (
            self.image_generation_timeout
            if self._active_image_generation
            else self.response_timeout
        )
        saw_new_response = False
        last_text = previous_text
        last_media_signature = previous_media_signature
        stable_since: Optional[float] = None
        next_status_report = start + 10.0
        image_output_seen = False
        image_notice_printed = False

        if self._active_image_generation:
            print(
                "Image-generation request detected. Waiting up to "
                f"{timeout:g} seconds for the response and image output."
            )

        print(
            "Response detector armed: "
            f"assistant_nodes={self._assistant_count()}, "
            f"generic_candidates={len(self._generic_message_items())}."
        )

        while True:
            state, information = self.detect_page_state()
            current_text = self.get_latest_assistant_text()
            assistant_count = self._assistant_count()
            generic_count = len(self._generic_message_items())
            # Search for explicit generated-image markers on every turn, not only
            # when the prompt heuristic predicted image generation. Natural prompts
            # like "visual of BIT" can still cause the model to generate an image.
            current_media_signature = self._generated_image_signature()

            structured_response_added = (
                previous_assistant_count is not None
                and assistant_count > previous_assistant_count
            )
            generic_response_added = (
                previous_generic_count is not None
                and generic_count > previous_generic_count
            )
            new_media_response = bool(
                self._media_keys(current_media_signature)
                - self._media_keys(previous_media_signature)
            ) and not image_output_seen
            if self._active_image_generation or new_media_response or image_output_seen:
                if new_media_response:
                    # An actual generated-image DOM marker is stronger evidence
                    # than prompt wording. Extend the wait even for implicit requests.
                    self._active_image_generation = True
                    timeout = max(timeout, self.image_generation_timeout)
                if new_media_response or image_output_seen:
                # get_latest_assistant_text() may fall back to an older non-empty
                # generic message when the new assistant turn contains only an
                # image. Once media appears, keep reading the current response
                # container on each poll so an old answer is never reused.
                    response_locator = self._latest_response_locator(
                        previous_assistant_count=previous_assistant_count,
                        previous_generic_count=previous_generic_count,
                        previous_text=previous_text,
                    )
                    if response_locator is not None:
                        current_turn_text = self._extract_element_text(response_locator)
                        if current_turn_text and self._normalize_text(current_turn_text) != self._normalize_text(previous_text):
                            current_text = current_turn_text
                        elif current_text == previous_text:
                            # Never return stale text from the preceding turn for an
                            # image-only response that has no readable message text.
                            current_text = ""

            new_text_response = bool(current_text and current_text != previous_text)

            if (
                structured_response_added
                or generic_response_added
                or new_text_response
                or new_media_response
            ):
                saw_new_response = True

            if new_media_response:
                image_output_seen = True
                if not image_notice_printed:
                    print(
                        "\n[IMAGE OUTPUT DETECTED] The model generated an image. "
                        "It is available in the browser. Waiting for the response "
                        "and image to finish..."
                    )
                    image_notice_printed = True

            # Some interfaces place image previews in dialog-like overlays. Once
            # an actual new image output is observed, don't misclassify that
            # preview as a request for user input.
            if state in {"RATE_LIMITED", "AUTH_REQUIRED", "USER_ACTION_REQUIRED"}:
                if not (
                    self._active_image_generation
                    and image_output_seen
                    and state == "USER_ACTION_REQUIRED"
                ):
                    return state, information or ""

            image_media_ready = bool(current_media_signature) and all(
                item.endswith("|ready") for item in current_media_signature
            )
            now = time.monotonic()
            if saw_new_response and (current_text or image_output_seen):
                content_changed = (
                    current_text != last_text
                    or current_media_signature != last_media_signature
                )
                if content_changed:
                    last_text = current_text
                    last_media_signature = current_media_signature
                    stable_since = now
                elif stable_since is None:
                    stable_since = now

                generation_active = self._is_generation_active()
                send_control_available = self._send_control_available()
                # A known-missing/disabled Send control is supplementary evidence
                # that the UI may still be processing. Unknown controls do not block
                # completion on providers with nonstandard composer layouts.
                submit_ui_not_ready = send_control_available is False
                if (
                    stable_since is not None
                    and now - stable_since >= self.response_stability
                    and not generation_active
                    and not submit_ui_not_ready
                    and (not image_output_seen or image_media_ready)
                ):
                    result = current_text.strip()
                    if image_output_seen:
                        image_note = (
                            "[IMAGE GENERATED] An image output is available in the "
                            "browser. Open the browser to view it."
                        )
                        result = f"{result}\n\n{image_note}" if result else image_note
                    return "COMPLETED", result
            else:
                stable_since = None

            if now >= next_status_report:
                print(
                    f"[response wait: {int(now - start)}s] "
                    f"assistant_nodes={assistant_count}, "
                    f"generic_candidates={generic_count}, "
                    f"latest_text_chars={len(current_text)}, "
                    f"generation_active={self._is_generation_active()}, "
                    f"send_control_available={self._send_control_available()}, "
                    f"image_output_detected={image_output_seen}"
                )
                next_status_report = now + 10.0

            if now - start >= timeout:
                state, information = self.detect_page_state()
                if state is not None and not (
                    image_output_seen and state == "USER_ACTION_REQUIRED"
                ):
                    return state, information or ""
                diagnostic = self._response_diagnostic(current_text)
                message = (
                    f"Timed out waiting for the response to finish after {timeout:g} seconds. "
                    f"Detector diagnostics: {diagnostic}"
                )
                print(f"[response timeout] {message}")
                if current_text or image_output_seen:
                    partial = current_text.strip()
                    if image_output_seen:
                        note = (
                            "[IMAGE OUTPUT DETECTED] The image may still be generating; "
                            "check the browser."
                        )
                        partial = f"{partial}\n\n{note}" if partial else note
                    # TIMEOUT_UNKNOWN is handled by the outer CLI without printing
                    # the returned result. Display any available image-turn text
                    # here so a long-running image operation doesn't hide it.
                    if self._active_image_generation and partial:
                        print("\n[PARTIAL MODEL RESPONSE BEFORE TIMEOUT]")
                        print(partial)
                    return "TIMEOUT_UNKNOWN", f"{partial}\n\n{message}"
                return "TIMEOUT_UNKNOWN", message

            time.sleep(self.poll_interval)


    def send_prompt(self, prompt: str) -> Tuple[str, str]:
        """Send a single prompt and return ``(state, response_or_information)``.

        A prompt is never blindly inserted a second time and a clicked Send
        control is never blindly clicked again. Ambiguous outcomes return an
        error so the caller can inspect the browser without duplicating a turn.
        """
        if not isinstance(prompt, str):
            return "SEND_ERROR", "Prompt must be a string."
        if not prompt.strip():
            return "SEND_ERROR", "Prompt cannot be empty."

        # Mark the intended user turn before taking response baselines. The prompt
        # is not in the transcript yet, so the initial counts still describe only
        # existing messages; after submission, generic extraction can consistently
        # exclude the outgoing prompt rather than miscounting it as a reply.
        self._active_prompt = prompt
        self._active_image_generation = self._is_image_generation_request(prompt)

        # Scale only browser interaction/verification budgets. Model response
        # generation keeps its independent response_timeout/image_generation_timeout.
        insertion_timeout = self._scaled_prompt_timeout(
            prompt, self.insertion_timeout, minimum=12.0, maximum=120.0
        )
        send_timeout = self._scaled_prompt_timeout(
            prompt, self.send_timeout, minimum=12.0, maximum=90.0
        )
        submission_timeout = self._scaled_prompt_timeout(
            prompt, self.submission_timeout, minimum=12.0, maximum=60.0
        )
        click_timeout_ms = int(
            self._scaled_prompt_timeout(
                prompt, 10.0, minimum=10.0, maximum=60.0
            ) * 1000
        )
        print(
            "Prompt-size timeouts: "
            f"insert/verify={insertion_timeout:.1f}s, "
            f"send-control={send_timeout:.1f}s, "
            f"submission-check={submission_timeout:.1f}s."
        )

        previous_text = self.get_latest_assistant_text()
        previous_assistant_count = self._assistant_count()
        previous_generic_count = len(self._generic_message_items())
        previous_media_signature = self._generated_image_signature()
        previous_user_count = self._user_message_count()

        # STEP 1: wait for a visible editable composer.
        composer = self._wait_for_composer(self.composer_timeout)
        if composer is None:
            self._active_prompt = None
            state, information = self.detect_page_state()
            if state in {"AUTH_REQUIRED", "RATE_LIMITED", "USER_ACTION_REQUIRED"}:
                return state, information or ""
            return "NOT_READY", "No visible, editable chat composer was found."

        # STEP 2: focus the selected composer.
        try:
            composer.click(timeout=5000)
        except Exception as error:
            self._active_prompt = None
            return "SEND_ERROR", f"Could not focus the chat composer: {error}"

        # STEP 3: insert once, then verify against one overall dynamic deadline.
        # A Playwright fill timeout does not prove that nothing was inserted: some
        # browser editors apply the text while their DOM is still updating. Never
        # return early merely because the composer is temporarily non-empty.
        print("Inserting prompt...")
        insertion_deadline = time.monotonic() + insertion_timeout
        fill_error: Optional[Exception] = None
        # Let fill attempt the operation, but reserve most of the prompt-size-scaled
        # budget for observing delayed UI updates and confirming the final text.
        fill_attempt_timeout = min(
            insertion_timeout,
            max(5.0, min(30.0, insertion_timeout * 0.35)),
        )
        try:
            composer.fill(prompt, timeout=int(fill_attempt_timeout * 1000))
        except Exception as error:
            fill_error = error

        remaining = max(0.0, insertion_deadline - time.monotonic())
        inserted = self._wait_for_prompt_in_composer(prompt, timeout=remaining)

        if not inserted and fill_error is not None:
            # Recover with keyboard insertion only when every visible composer is
            # empty. If any composer contains text, treat it as an ambiguous/ongoing
            # insertion and do not append or reinsert anything.
            visible_texts: List[str] = []
            try:
                candidates = self.page.locator(_COMPOSER_SELECTOR)
                for index in range(candidates.count()):
                    candidate = candidates.nth(index)
                    try:
                        if not candidate.is_visible():
                            continue
                        data = candidate.evaluate(
                            """el => ({
                                value: typeof el.value === 'string' ? el.value : '',
                                innerText: typeof el.innerText === 'string' ? el.innerText : '',
                                textContent: typeof el.textContent === 'string' ? el.textContent : ''
                            })"""
                        )
                        visible_texts.extend(
                            self._normalize_text(data.get(key, ''))
                            for key in ('value', 'innerText', 'textContent')
                        )
                    except Exception:
                        continue
            except Exception:
                pass

            any_nonempty = any(value for value in visible_texts)
            remaining = max(0.0, insertion_deadline - time.monotonic())
            if not any_nonempty and remaining > 0:
                current_composer = self._wait_for_composer(remaining)
                if current_composer is not None:
                    try:
                        current_composer.click(timeout=min(3000, max(500, int(remaining * 1000))))
                        self.page.keyboard.insert_text(prompt)
                    except Exception as error:
                        self._active_prompt = None
                        return "SEND_ERROR", f"Could not insert prompt after an empty-composer recovery: {error}"
                    remaining = max(0.0, insertion_deadline - time.monotonic())
                    inserted = self._wait_for_prompt_in_composer(prompt, timeout=remaining)

        if not inserted:
            self._active_prompt = None
            diagnostic = getattr(self, "_last_prompt_verification", {})
            observed = diagnostic.get("observed", [])
            observed_summary = ", ".join(
                f"{item.get('tag', '?')} lengths={item.get('lengths', [])}"
                for item in observed
            ) or "no visible composer text was readable"
            fill_summary = (
                f" Fill attempt error: {type(fill_error).__name__}: {fill_error}."
                if fill_error is not None
                else ""
            )
            return (
                "SEND_ERROR",
                "The complete prompt was not verified before the dynamic insertion deadline. "
                f"Expected normalized characters={diagnostic.get('expected_chars', 'unknown')}; "
                f"observed={observed_summary}. The Send control was not clicked."
                f"{fill_summary}",
            )

        verification = getattr(self, "_last_prompt_verification", {})
        if verification.get("verification") == "all_expected_chars_present_with_small_editor_extras":
            print(
                "Prompt inserted and verified; all expected content is present "
                f"(editor exposed {verification.get('extra_observed_characters', 0)} additional character(s))."
            )
        else:
            print("Prompt inserted and verified.")

        # STEP 4: keep searching until a related Send/submit control is visible
        # and enabled. Its label or test ID is not required.
        print("Waiting for the Send control to become available...")
        send_deadline = time.monotonic() + send_timeout
        send_button: Optional[Locator] = None

        while time.monotonic() < send_deadline:
            composer, _score = self._find_composer_on_page(
                self.page, require_ready=False
            )
            if composer is None:
                time.sleep(self.poll_interval)
                continue
            candidate, enabled = self._find_send_button(composer)
            if candidate is not None and enabled:
                send_button = candidate
                break
            time.sleep(self.poll_interval)

        if send_button is None:
            self._active_prompt = None
            return (
                "SEND_ERROR",
                "No enabled submit control associated with the composer became available. "
                "The prompt remains in the composer and was not submitted.",
            )

        # STEP 5: click exactly once, then verify acceptance rather than retrying.
        #print("Submitting prompt...")
        submission_confirmed = False
        try:
            send_button.click(timeout=click_timeout_ms)
        except Exception as error:
            # The click may have reached the page despite a timeout. Inspect state
            # but never click a second time automatically.
            submission_confirmed = self._wait_for_submission(
                prompt,
                previous_user_count,
                previous_text,
                previous_assistant_count,
                previous_generic_count,
                timeout=submission_timeout,
            )
            if not submission_confirmed:
                self._active_prompt = None
                return (
                    "SEND_ERROR",
                    "The submit click could not be confirmed: "
                    f"{error}. The prompt was not automatically resent; check the browser.",
                )

        #print("Verifying that the prompt was accepted...")
        if not submission_confirmed and not self._wait_for_submission(
            prompt,
            previous_user_count,
            previous_text,
            previous_assistant_count,
            previous_generic_count,
            timeout=submission_timeout,
        ):
            self._active_prompt = None
            return (
                "SEND_ERROR",
                "The submit control was clicked, but the page did not confirm acceptance. "
                "The request was not retried to avoid sending a duplicate prompt.",
            )

        print("Submission accepted. Waiting for the model response...")
        try:
            return self.wait_for_response(
                previous_text,
                previous_assistant_count=previous_assistant_count,
                previous_generic_count=previous_generic_count,
                previous_media_signature=previous_media_signature,
            )
        finally:
            self._active_prompt = None
            self._active_image_generation = False

