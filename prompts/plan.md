# Plan

Turn the survey map into an execution plan for the engineer who will do the
work. You may re-check a fact with `bash` or `read_file`, but do not change any
file.

The plan must be specific enough to follow without re-reading this
conversation:

1. **Deliverables**: one line per graded path - path, format, and a checklist of
   what must be inside it (tables, sheets, sheets of a drawing, model elements,
   memo sections). This checklist is verified later, so make it verifiable.
2. **Steps**: an ordered list of engineering steps with the file each step
   reads, the tool or script it runs, and the value or artifact it produces.
   Put the biggest scoring items first, so a truncated run still scores.
3. **Checks**: the numerical and format checks to run before finishing, each
   with the expected value or tolerance where one can be computed.
4. **Risks**: what is most likely to be wrong, and the fallback if a solver or
   app misbehaves.

Keep it under 120 lines.
