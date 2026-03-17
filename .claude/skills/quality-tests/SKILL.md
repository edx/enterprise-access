---
name: quality-tests
description: Run code quality checks (linting, style, PII annotations) in the enterprise-access Docker container. Use when the user wants to run quality checks, lint code, or verify code style compliance.
allowed-tools: Bash(docker *), Bash(make *)
---

## Steps

### 1. Make sure the app container is running

Determine if the enterprise-access.app container is running:

```bash
docker ps | grep enterprise-access.app
```

Start the app container if not running:

```bash
make dev.up
```

### 2. Run quality checks

```bash
docker exec enterprise-access.app bash -c "make quality"
```
