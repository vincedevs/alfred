---
name: writing-skills
description: How to write or fix a skill. Use when the user teaches you a procedure, asks you to learn something, or you worked out how to do something you will need again.
---

# Writing a skill

A skill is a folder in your skills folder (the folder this file is in, one level up):
`<skill-name>/SKILL.md`, plus any scripts or reference files it needs.

1. Check the existing skills first: improve one that covers the task rather than adding a
   near-duplicate.
2. Name it with lowercase words and hyphens: `deploy-portfolio`, `pi-wifi-switch`.
3. Start SKILL.md with this frontmatter, then the steps:

   ```
   ---
   name: deploy-portfolio
   description: What it does and when to use it, in one sentence.
   ---
   ```

   The description is how you find the skill later: say when to use it, with the words
   the user would say.
4. Write the steps as you would for a capable colleague: the commands that worked, the
   order, the checks that show it worked, and the mistakes to avoid. Keep it short.
5. Put long reference material or reusable scripts in their own files next to SKILL.md
   and say when to read or run them.
6. Never put passwords, keys or tokens in a skill.
7. Saving a skill waits for the user's yes. Afterwards, say in one sentence what you saved.
