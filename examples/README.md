# Examples

The CLI consumes paths rather than benchmark-shaped packages. A minimal invocation is:

```bash
semantic-terraform-agent diagnose \
  --repo-path /workspace/my-service \
  --terraform-dir deploy/terraform \
  --log-file /tmp/terraform-plan.stderr.log \
  --provider gemini \
  --model gemini-3.6-flash \
  --context-mode auto \
  --verify-patch \
  --max-repair-attempts 1 \
  --output /tmp/terraform-diagnosis.json
```

Add `--diff-file /tmp/change.patch` to make the comparison deterministic. Unified-diff
paths must be relative to `--repo-path` (normally `a/...` and `b/...`). Without a supplied
diff, the result JSON records the local Git comparison used. Patch verification is enabled
by default and runs only in a filtered temporary copy; pass `--no-verify-patch` to record it
as intentionally skipped. At most one repair is allowed; pass `--max-repair-attempts 0` to
disable it while keeping first-patch verification.
