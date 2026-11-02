# Reviewer output contract

Every gate reviewer ends its reply with **one** fenced JSON block in exactly this shape. `gate.py` parses
the last ```json block of the reply; anything else in the reply is for humans.

```json
{
  "reviewer": "<your reviewer id, as given>",
  "tree": "<the tree id you were given, copied exactly>",
  "checked": ["<every file you read end to end, path relative to the repository root>"],
  "findings": [
    {
      "id": "<your lens's rule id, e.g. MV1, or the allowlist id for a rules review>",
      "severity": "blocker | should-fix | nit",
      "file": "<path relative to the repository root, or the absolute path you were given>",
      "line": 42,
      "quote": "<the offending text, copied exactly from that file at that line>",
      "why": "<one sentence: what is wrong>",
      "fix": "<the smallest change that resolves it>",
      "plain": "<one short sentence in easy English for someone who has not read the code: what goes wrong for the person using it. No file, function or variable names>"
    }
  ]
}
```

Rules the script enforces. Breaking one makes your whole review invalid, or drops the finding:

- **`checked` must list every file in your scope.** A file you were given but did not list makes the
  review invalid; it is re-run.
- **`quote` must be real.** It is searched for within 3 lines of `line` in `file`, ignoring whitespace
  differences. A quote that is not there is dropped as unverified. Quote one or two lines, never a paraphrase.
- **`tree` must be the id you were given.** A wrong tree makes the review invalid.
- **No findings is a valid answer.** `"findings": []` means you found nothing. Never invent a finding to look busy.

Severity:

| severity | meaning | effect |
| --- | --- | --- |
| blocker | wrong behaviour, a broken rule, a missing ask, a test that no longer proves anything | blocks the gate |
| should-fix | a real defect of your lens that a careful reviewer would not let merge | blocks the gate |
| nit | taste, or a minor improvement | listed in the MR, never blocks |

Keep the prose before the block under 400 words, and report at most 10 findings, most severe first.
