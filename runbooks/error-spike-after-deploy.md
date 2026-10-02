# Error spike after a deploy

Symptoms: 5xx rate rises within minutes of a new revision serving traffic.
Unhandled exceptions (TypeError, KeyError, AttributeError) with stack traces in
the new revision, none in the old one.

Check:
- list_revisions: which revision is serving, when it was created, the commit message.
- Do the errors only appear on the new revision? Compare the `revision` field.
- Does the stack trace point at code the commit touched?

Fix: roll back to the last good revision (the one serving before the spike).
Needs human approval. Afterwards, open a bug against the commit.
