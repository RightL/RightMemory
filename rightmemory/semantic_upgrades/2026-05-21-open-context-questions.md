---
id: open-context-questions
introduced_at: 2026-05-21
---

# Open Context Questions

Ensure the memory root has a global `# Open Context Questions` section.

Create the section if it is missing, even when there are no questions to add yet. Use this exact heading and body:

```md
# Open Context Questions {#open-context-questions}

This section stores loose ends in memory as short questions for future agents. These questions are not declarative memory facts; they point to related memory with `todo:` and should be removed or revised after the answer is saved as ordinary memory.
```

Review existing memory for loose ends: areas that feel incomplete, unclear, or hard to apply because something still needs to be pinned down. When a loose end seems worth surfacing later, add a short question node under `# Open Context Questions` and link it to the related memory with `todo:`.

Do not copy answers into the question section. When a question is already answered by existing memory, make sure the answer is represented in the appropriate declarative memory section and omit the question.
