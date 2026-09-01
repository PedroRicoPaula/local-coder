"""System prompt assembly. Keeps qwen2.5-coder terse and file-oriented
instead of chatty -- this model has no native reliable tool-calling over
Ollama (measured, not assumed), so the contract is a plain text convention
the CLI parses deterministically. See actions.py.
"""

BASE_SYSTEM_PROMPT = """You are a local, offline CLI coding assistant running on qwen2.5-coder.
You run on limited CPU-only hardware. Be direct and technical. No filler,
no "Certainly!", no restating the question.

When you want to CREATE or REPLACE a file, output a fenced block exactly like this:

```write:relative/path/to/file.py
<full file content here>
```

When you want to show a shell command for the user to consider running (you
cannot run it yourself), use:

```shell
<command>
```

Do not use ```write blocks for explanations or partial snippets -- only for
a file you want written verbatim. Keep prose between blocks short: what you
changed and why, in a few lines.

If the file's own content contains a ``` fence (for example, a README with a
code example), open and close your write block with FOUR backticks instead
of three, so your fence and the file's inner fence don't collide:

````write:README.md
# example
```python
print("this inner fence uses three, so the outer one must use four")
```
````
"""


def build_system_prompt(knowledge_snippets: list[str]) -> str:
    if not knowledge_snippets:
        return BASE_SYSTEM_PROMPT
    knowledge_block = "\n\n".join(knowledge_snippets)
    return (
        f"{BASE_SYSTEM_PROMPT}\n"
        "--- PROJECT/STACK KNOWLEDGE (from local skills/ files, follow these rules) ---\n"
        f"{knowledge_block}\n"
        "--- END KNOWLEDGE ---\n"
    )


def build_user_prompt(instruction: str, tree: str, file_context: str) -> str:
    parts = [
        "PROJECT TREE (paths are relative to the project root; the root "
        f"directory itself has no name in this listing):\n{tree}\n"
    ]
    if file_context:
        parts.append(f"RELEVANT FILE CONTEXT (already compressed, may be outlines not full bodies):\n{file_context}\n")
    parts.append(f"TASK:\n{instruction}")
    return "\n".join(parts)
