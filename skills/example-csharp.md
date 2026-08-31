# C# house rules

- Nullable reference types enabled; no `!` suppression without a comment
  explaining why it's safe.
- Async all the way down: no `.Result` or `.Wait()` on a Task.
- Prefer records for immutable data.

Delete this file (or example-python.md) if you don't work in this stack --
every file here gets injected into every prompt, so keep only what you use.
