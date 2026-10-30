"""Thread-safe bridge for runtime events entering the Textual UI."""

from application.contracts import RuntimeEventType
from textual.message import Message


class RuntimeEventMessage(Message):
    """Textual message envelope for one application/runtime event.

    Textual's ``post_message`` is thread-safe. A future Gateway Runtime may
    call ``RelayPilotApp.post_runtime_event(event)`` without touching widgets.
    The event is handled by the app on the UI thread.
    """

    def __init__(self, event: RuntimeEventType) -> None:
        super().__init__()
        self.event = event
