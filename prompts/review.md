Review pull request #{n} of {repo} and return a structured verdict. You are an unattended run: nobody reads this while it runs, so never ask a question.
- You have no shell and no network. Everything is prepared in {inputs}:
  input.diff (git diff BASE_SHA..HEAD_SHA), pr.md (title and body; a linked ticket is not reachable, work from
  the PR body and say so if it matters){notes}, and reviewer-prompt.md, the complete brief for your reviewer.
- The repository is checked out at HEAD_SHA in the current directory; read any file for context.
- BASE_SHA = {base}. HEAD_SHA = {head}.{range_note}{rereview}{brain}
- First read pr.md and only the file headers of input.diff (`diff --git` lines), not the diff itself: that is
  the reviewer's job, and every line you read is re-sent with each of your later calls. Then write three to
  eight questions this review must settle: what could break in production, what the description promises that
  the diff may not deliver, what the project context flags for these files. Phrase them as questions, never
  as your conclusions.
- Dispatch exactly one reviewer with the Agent tool: subagent_type "general-purpose", model "{reviewer}", effort "high", in
  the foreground, with a prompt that says to read {inputs}/reviewer-prompt.md and follow it, followed by your
  numbered questions.
- Then open the lines behind every critical and important finding yourself. Drop a finding the code shows to
  be wrong; keep one you could not settle at its severity, marked inferred. Do not re-review the rest.
- Plain punctuation, no em or en dashes, blameless.
Return the structured result: verdict "approve" only when the change is ready to merge with zero critical and
zero important findings, otherwise "comment"; a summary of one or two sentences (why, and what must happen
before what); strengths (what is done well, briefly and specifically); not_judged (each part not reviewed in
depth and each question not settled, one line with the reason; empty when there is none); every finding with
severity, file, line, title and detail. The approval is decided from the findings alone, not from the
verdict word: anything that should stop this change from merging, including something you could not
verify, must be a critical or important finding.
