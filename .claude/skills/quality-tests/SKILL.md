---
name: quality-tests
description: Run code quality checks (linting, style, PII annotations) in the enterprise-access Docker container. Use when the user wants to run quality checks, lint code, or verify code style compliance.
allowed-tools: Bash(docker *), Bash(make *), Bash(colima *)
---

## Steps

### 1. Make sure the app container is running

Determine if the app container is running:

```bash
docker compose ps
```

Start the app container if not running:

```bash
make dev.up
```

### 2. Run quality checks

```bash
docker compose exec app bash -c "make quality"
```

## Troubleshooting

### ModuleNotFoundError

If quality checks fail due to missing imports, first try to install requirements:

```bash
docker compose exec app make ci_requirements
```

This is necessary at least when adding new requirements which have not yet been built into the image.

### Failed to connect to the docker API on MacOS

This likely just means colima needs to be started:

```bash
colima start
```
