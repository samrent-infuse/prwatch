Review pull request #{n} of {repo} following the superpowers requesting-code-review skill:
read {skill}/SKILL.md and its template {skill}/code-reviewer.md, and follow them, with these adaptations.
- You are an unattended run: nobody reads this while it runs, so never ask a question.
- You have no shell and no network. Everything is prepared in {inputs}:
  input.diff (git diff BASE_SHA..HEAD_SHA), pr.md (title and body: DESCRIPTION and PLAN_OR_REQUIREMENTS;
  a linked ticket is not reachable, work from the PR body and say so if it matters){notes}.
- The repository is checked out at HEAD_SHA in the current directory; read any file for context.
- BASE_SHA = {base}. HEAD_SHA = {head}.{range_note}{rereview}{brain}
- Before dispatching, read pr.md and the list of changed files at the top of each file section of input.diff
  (not the whole diff: that is the reviewer's job), and write three to eight questions this review must
  settle: what could break in production, what the description promises that the diff may not deliver, what
  the project context flags for these files. Phrase them as questions, never as your conclusions.
- Dispatch exactly one reviewer subagent with model "{reviewer}" and effort "high". Give it the paths above, the
  project context, your questions (to answer each with the line it checked), and these working rules:
  every tool call re-sends its whole context, so aim for about 25 tool calls and stop at 40; read input.diff
  in slices of 300 to 600 lines and a whole function per read; stay within this PR; verify each finding
  against the code, quoting the line and saying whether it is confirmed or inferred; list what it did not
  review in depth, and questions it could not settle, under "Not judged".
- Then open the lines behind every critical and important finding yourself. Drop a finding the code shows to
  be wrong; keep one you could not settle at its severity, marked inferred.
- Plain punctuation, no em or en dashes, blameless.
Return the structured result: verdict "approve" only when the change is ready to merge with zero critical and
zero important findings, otherwise "comment"; a short summary that ends with one line "Not judged: ..." (or
"Not judged: nothing"); every finding with severity, file, line, title and detail. The approval is decided
from the findings alone, not from the verdict word: anything that should stop this change from merging,
including something you could not verify, must be a critical or important finding.
