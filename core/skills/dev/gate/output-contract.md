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
      "plain": "<one short sentence in easy English for someone who has not read the code: what goes wrong for the person using it. No file, function or variable names>",
      "older": "<optional: true when the defect was already in the code and this change neither caused nor worsened it>"
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
| blocker | wrong behaviour in the main use, a broken project rule, a missing ask, a test that no longer proves anything | blocks the gate |
| should-fix | a real defect: a wrong result in a case real callers can hit, changed behaviour with no test that would fail without it, a doc the change made false | blocks the gate |
| nit | not a defect: naming, constants, style, duplication, a missing note in the divergence log | listed, never blocks |

This table overrides any severity in your lens's own table. For a blocker or a should-fix, `why` must name the concrete failure: this input or step, this wrong result. If you cannot name one, it is a nit.

Report a real defect even when this change did not cause it, and set `older` to true. It costs no attempt; the user decides whether to fix it.

A clean change has no findings, and that is the expected answer. Keep the prose before the block under 100 words, and report at most 10 findings, most severe first.
