# Secrets setup

Values never go into Slack, commits, logs or receipts. Only the four names below are known
to the repository.

## GitHub Actions repository secrets

```bash
# run from a machine with repo admin rights; gh prompts for the value, nothing lands in shell history
gh secret set HF_TOKEN_MAIN   -R duneon97/evidence-gated-repair
gh secret set HF_TOKEN_FALLBACK -R duneon97/evidence-gated-repair
gh secret list -R duneon97/evidence-gated-repair      # names only, values are write-only
```

## InstaBox VM (`/data/.env`)

```bash
install -m 600 /dev/null /data/.env
cat >> /data/.env <<'EOF'
HF_TOKEN_MAIN=
HF_TOKEN_FALLBACK=
HF_BASE_URL=https://router.huggingface.co/v1
MODEL_ID=meta-models/Muse-Spark-Open-816B-A42B
EOF
chmod 600 /data/.env
```

Fill the two token values with an editor, not with `sed` on the command line. Verify the file
stays ignored:

```bash
git check-ignore -v /data/.env      # /data is outside the repo, but keep .env ignored inside it too
git check-ignore -v .env
```

## Runtime rules

- The client reads `HF_TOKEN_MAIN`, then falls back to `HF_TOKEN_FALLBACK` on HTTP 429 and 5xx.
- Never log the token. Log only `key_env` and a boolean `key_present` (that is what
  `tools/probe_model.py` reports).
- Receipts record model id, provider and token cost — never credentials or headers.
- Sandbox containers get `--network none` and no secrets mounted; the model call happens on the
  host side, the test run happens inside the container.
