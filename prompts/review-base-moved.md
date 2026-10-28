- The PR's base moved since the last review (a rebase, a force push or a merge from {branch}), so input.diff
  is the whole PR from its merge-base, not only the new commits. When compare.txt is present it compares the
  whole patch at the reviewed head with the patch now, line by line: PATCH_SUBSET means lines were only
  dropped, PATCH_CHANGED lists the lines only in the new patch first. If every such line is behavior-neutral
  (read each one in context) and previous.md's verdict still holds, do not dispatch a reviewer: return the
  earlier findings as they stand and say in the summary that the patch is unchanged apart from those lines.
