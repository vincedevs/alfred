"""The benchmark's prompts: Alfred's own (prompts/*.md), read as they are, plus a note that
only web search is on. Nothing is copied, so later edits reach the benchmark too."""

from alfred.config import load_prompt

THIS_SESSION = (
    "\n\n## In this session\nYou have web search only: the agent, memory and skills are "
    "off. If the user asks for one, say briefly that it isn't available right now."
)


def benchmark_prompt(name: str) -> str:
    return load_prompt(name) + THIS_SESSION
