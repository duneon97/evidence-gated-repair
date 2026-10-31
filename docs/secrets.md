# Secret handling

Never put credentials in commits, command-line arguments, screenshots, or receipts.
The checked-in `.env.example` contains variable names with empty values only.

```bash
cp .env.example .env
chmod 600 .env
# Fill values privately in an editor, not through shell history.
git check-ignore -v .env
```

The repair agent reads `.env` by default (`--env-file` overrides it).
Nonempty process environment settings override the file; CLI provider/model/base
overrides take precedence. Empty entries use defaults.

| Name | Use |
| --- | --- |
| `MODEL_PROVIDER` | `hf` (default), `llama`, or explicit offline `mock` |
| `MODEL_ID` | HF defaults to Meta Llama; required for the optional native Meta API |
| `HF_TOKEN_MAIN` | Main HF router credential |
| `HF_TOKEN_FALLBACK` | Optional fallback HF credential |
| `HF_BASE_URL` | Optional HF API root override |
| `LLAMA_API_KEY` | Optional native Meta Llama API credential |
| `LLAMA_API_BASE` | Required HTTPS API root for `llama`, including version prefix |

Runtime rules:

- Real providers never switch to mock on missing credentials or HTTP errors.
- Record only credential variable names, booleans, HTTP status, timing, and available
  usage. Do not echo provider error bodies, headers, credentials, or configurable URLs.
- Model requests run on the host. Docker test processes receive no provider environment
  variables; dotenv files are excluded from repository copies.
- Local subprocesses remove provider credentials from their environment, but this
  is **NOT a sandbox**: code can still read host files or use the network.
- Target repositories must not contain secrets in any other files. Prompts, source
  excerpts, and test logs can contain repository content; inspect receipts before sharing.
- The offline demo needs no env file, credentials, or API access.

The historical probe scripts in `tools/` and the manual CI workflow use `HF_TOKEN`
for a single probe credential. They are separate from the repair agent's
`HF_TOKEN_MAIN` / `HF_TOKEN_FALLBACK` configuration.
