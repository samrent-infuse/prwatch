Copy to project-context.md (git-ignored). Every review gets it as project-context.md, in place of the
repository's own CLAUDE.md, which a review run does not load. Say what a reviewer must know about the
repository and cannot see from the diff. Keep it short and current. For example:

- Which environment is production, and whether a merge deploys anything by itself.
- Changes that need a manual step after merge (infrastructure, migrations), and what the PR must say about it.
- How the schema reaches production, and its traps.
- Incident shapes this codebase has had, by name, so the reviewer checks for them.
- Conventions a change must follow (vocabulary, comments, logging), and test-suite rules.
