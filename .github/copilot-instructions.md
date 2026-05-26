# Copilot instructions
These rules apply to all Copilot-assisted changes and reviews in this repository.

## Code Review Standards

### Core principles
* Prioritize signal over noise
* Be direct and concise
* Prioritize security, data integrity, and correctness
* Assume separate CI actions run lint and style checkers

### Output format
* Start with a concise summary of changes (one or two sentences).
* If no critical issues exist, include the sentence: `No critical issues found.` before listing any non-critical issues.
* If no issues (critical or non-critical) exist at all, output exactly: `No issues found.`
* Incremental changes to PR: when reviewing follow-up commits/PR updates, prioritize issues in newly changed files/lines before re-scanning untouched legacy code.

### Security
* Never output hardcoded secrets/keys; if encountered, flag them immediately.
* Sensitive Logging: immediately flag logging statements that might expose potential PII or secrets (tokens, passwords, connection strings, API keys).
* Least Privilege: Default to restrictive permissions and visibility.
