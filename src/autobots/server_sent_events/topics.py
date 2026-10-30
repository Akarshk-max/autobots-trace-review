"""The topics a run publishes on.

A run publishes on its own id, and its text on a suffixed variant of it. The
suffixes were spelled inline at each of the nine places that build one, and
again in the stream gate, which is the only reader — so a tenth suffix could be
added to a provider and the gate, knowing nothing of it, would refuse to let
anyone subscribe. Producers and gate now read from here.
"""

from typing import Optional

TEXT = "-text"

# openai_chat_stream publishes a second, complimentary stream alongside the
# first, and is the only thing that does.
TEXT_COMPLIMENTARY = "-text-complimentary"

# Longest first, so a topic is never left holding half a suffix.
SUFFIXES = (TEXT_COMPLIMENTARY, TEXT)


def text_topic(topic: str) -> str:
    return f"{topic}{TEXT}"


def text_complimentary_topic(topic: str) -> str:
    return f"{topic}{TEXT_COMPLIMENTARY}"


def base_topic(topic: str) -> Optional[str]:
    """The run topic a suffixed one belongs to.

    Returns the topic unchanged when it carries no suffix, since a run's own id
    is a topic in its own right.
    """
    if not topic:
        return None
    for suffix in SUFFIXES:
        if topic.endswith(suffix):
            return topic[: -len(suffix)]
    return topic
