# RightMemory Edit Corrections: Example

This file illustrates the correction format only. Its ids and content are not user state.

## Keep unfinished work in Pursuit

### Candidate

```text
The parser change is implemented, but compatibility tests still fail. Continue by fixing the remaining failures.
```

### Proposed edit

`MEMORY.md`:

```md
- `parser-upgrade-complete` The parser upgrade is complete and verified. →[]
```

### Accepted edit

`MEMORY.md`:

```text
[no change]
```

`PURSUITS.md`:

```md
## Finish parser compatibility work {#finish-parser-compatibility}

Complete the parser upgrade without leaving compatibility regressions.

**State:** The implementation is present, but compatibility tests still fail.

**Next:**
- `do` Fix the remaining compatibility failures and rerun the affected tests.

**Done when:** The compatibility test suite passes.
```
