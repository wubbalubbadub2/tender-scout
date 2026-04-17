import anthropic
from prompts import SYSTEM_PROMPT, USER_TEMPLATE

MODEL = "claude-sonnet-4-6"
MAX_TEXT_CHARS = 150_000

_client = anthropic.Anthropic()


def analyze(text: str, link: str) -> str:
    trimmed = text[:MAX_TEXT_CHARS]
    message = _client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": USER_TEMPLATE.format(link=link, text=trimmed)}
        ],
    )
    return message.content[0].text
