Read CLAUDE.md fully before doing anything else.

Then do the following and present the results clearly:

1. Show the current state section from CLAUDE.md
2. Run: git log --oneline -10
   Summarise what was last worked on in one sentence.
3. Run: gh issue list --repo tlo300/photo-platform --label in-progress --json number,title
   List anything currently in progress.
4. Run: gh issue list --repo tlo300/photo-platform --milestone "1 - Foundation and infrastructure" --state open --json number,title,labels
   (Replace the milestone name with the active one from CLAUDE.md if different.)
   List the open issues in the current milestone.
5. Based on the above, recommend the single best next issue to work on and why.

Then wait for confirmation before starting any work.
