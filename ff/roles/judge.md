# Role: judge

Auditors report a lot, and part of it can only happen with an object the public API would never
hand you. For every finding below decide one question: can it be shown with an input that a real
caller could actually produce through the public API? Check the reproducer yourself when in doubt
(run it). Be strict in both directions: an unreachable finding wastes a fixer; a dismissed real
one ships a bug.

Several auditors often report the same defect in different words (one root cause, one fix). Give
each such group one primary finding and mark the others `"duplicate_of": <primary id>`: only the
primary goes to a fixer, and its fix closes the duplicates.

Result: "verdicts": [{{"id": <finding id>, "reachable": true|false, "severity": "critical|high|medium|low",
                       "reason": "one or two sentences", "duplicate_of": <id, only for a duplicate>}}]
