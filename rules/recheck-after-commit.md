Every time you make a git commit, check whether you should run `/recheck`:

1. Skip if the commit message contains `[rechecker: skip]` — that commit was already made by the rechecker itself.
2. Count the files in the commit you just made (`git show --name-only --format= HEAD`). Only run `/recheck` if **5 or more files** were committed in that single commit, or the total size of those files is **50 KB or more**.
3. If the threshold is met, run `/recheck` immediately after the commit.
