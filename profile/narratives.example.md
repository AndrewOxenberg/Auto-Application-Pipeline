# Narratives

Copy this to `narratives.md` and replace it with your own.

    cp profile/narratives.example.md profile/narratives.md

Reusable prose for the questions every application asks in slightly different
words. Drafting pulls from here and swaps in a job-specific hook, so these
should read like you, not like a cover letter template.

Every block lists the `evidence.yaml` ids behind it. That is not decoration:
the grounding validator checks generated text against those ids, and a claim
with no id backing it blocks the draft.

---

## The core story: why you build things

*Evidence: `example-realtime-system`, `example-algorithm`*

One paragraph on what makes you reach for a keyboard. The strongest version of
this is concrete and slightly stubborn: a specific thing that bothered you, and
what you built because of it. Avoid "passionate about technology".

**Use for:** "why do you want to build software", "tell me about a project",
"what are you proud of".

---

## Ownership under ambiguity

*Evidence: `example-ownership`, `example-algorithm`*

A time nobody told you what to do and you shipped anyway. Name the decision you
made that a less careful person would have skipped.

**Use for:** "a time you worked without direction", "a hard technical problem".

---

## How you know your work is right

*Evidence: `example-realtime-system`*

Your relationship with being wrong. Test counts, reproductions, the bug you
caught because you checked. This one separates people more than it looks like
it should.

**Use for:** "how do you know your code is correct", "tell me about a bug".

---

## Leadership that shows up in the work

*Evidence: `example-leadership`*

Not the title. What changed because you were there, and how it shows up in how
you take feedback.

**Use for:** "leadership experience", "a conflict you resolved".

---

## Known gaps — state these honestly, never paper over them

List what you do not have. This section exists so that tailoring cannot quietly
claim it.

- No Go, Kotlin or Scala.
- No production distributed-systems experience at scale.
- No cloud certification or production deployment.

The pipeline surfaces gaps per posting in the fit score. Do not let a generated
draft claim any of these.
