## What changes

<!-- One paragraph, or one sentence if the change is small. The reader should
     be able to tell what changed without opening the diff. -->

## Why

<!-- The reason, not the mechanism. If a rule in the house style is being
     broken here, say which one and why it had to be broken. A deliberate
     exception is fine, an unnoticed one is not. -->

## How to check it

<!-- The command a reviewer runs to verify this. If there is no such command,
     say so; that is worth knowing too. -->

## Checklist

- [ ] `scripts/housecheck.py --strict .` passes.
- [ ] Tests pass, and new behaviour comes with a test.
- [ ] `README.md` describes its current behavior.
- [ ] `CHANGELOG.md` has an entry, if the change is visible to a user.
- [ ] No new dependency without a reason written down next to it.
