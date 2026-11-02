# Third-party material

## agent-shell-hamelech (MIT)

Source: https://github.com/AdirD/agent-shell-hamelech

Adapted, rewritten for this gate:

- `skills/melech-tidy/SKILL.md`: the inverted burden of proof (every added symbol is residue until
  proven needed), its reachability, requirement and non-duplication proofs, and the fail-closed rule
  for diff noise. Used in `core/reviewers/polish.md` (LN1–LN4).
- `skills/melech-roi/SKILL.md`: proportionality of blast radius to benefit, "are we rerouting the
  house, or replacing the washer?". Used in `polish.md` (LN5) and `plan.md` (P3).
- `skills/melech-pre-plan/SKILL.md` and `skills/melech-challenge/SKILL.md`: the question filter and the
  simplicity bias ("duplication is acceptable when it keeps blast radius lower"). Used in `plan.md`.
- `skills/melech-consult/SKILL.md`: "artifact, never narration" and "Do not defer to the answer the
  framing seems to expect." Used in `dispatch-prompt.md`.
- `scripts/pre-push-audit.sh`: a secrets regex and agent-directory check as a hard exit-code gate.
  Used in `gate.py audit`.

```
MIT License

Copyright (c) 2026 AdirD

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## everything-claude-code (licence unconfirmed)

Source: https://github.com/worldflowai/everything-claude-code. Its LICENSE file returned 404, so
**ideas only, no text**:
- block on the two highest severities (here: blocker and should-fix);
- a per-reviewer "not a finding" list;
- mechanical checks first, stop on the first failing group.
