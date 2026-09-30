# Cloud daily-report delivery adapter

This optional Linux/Python adapter reuses the standalone attention-report job and
prepares one email payload per completed `Asia/Shanghai` calendar day. It does not
send mail, install a scheduler, or modify the existing Windows/Docker workflow.
The mail provider and durable storage are separate integrations.

## Setup

Run from this repository's root with Python 3.10 or later:

```sh
bash cloud/bootstrap.sh
export BILI_ATTENTION_RECIPIENT='recipient@example.com'  # replace with your address
```

The bootstrap creates a separate `.venv-cloud`, installs the pinned minimal
dependencies, and copies `config/attention.deployment.example.json` to the ignored
`config/attention.deployment.json` only when the destination is absent. Review the
local configuration before use. The example's `07:30` time is metadata for an
external scheduler; running the wrapper manually is not time-gated.

The recipient must be a single plain email address. It is required for every
runner invocation, including scheduled invocations, and must match the existing
ledger. There is no default recipient. Changing the recipient requires a separate
deployment/ledger rather than reusing another recipient's delivery history.

Only for a genuinely new deployment, initialize an empty ledger:

```sh
.venv-cloud/bin/python cloud/runner.py init-ledger
```

This command refuses to overwrite an existing ledger. If a previous deployment
has sent mail, restore its latest durable `state/run-ledger.json` backup instead.
Never initialize a replacement ledger just because a workspace or file was lost.

The account owner must run the following command themselves in a trusted
terminal. Do not put credentials in chat, command-line arguments, logs, Git,
deployment archives, or a code backup:

```sh
.venv-cloud/bin/python cloud/enter_credentials.py
```

The helper asks for consent and then uses hidden terminal input for the Bilibili
`SESSDATA` value. It writes an owner-only `secrets/bilibili.env` file (0600) in an
owner-only directory (0700), and refuses symbolic-link targets. Credential input
is refused if a terminal cannot provide hidden input. Runtime `data/` and
`state/` directories are also owner-only (0700). For a restored or separate-volume
layout, ensure those permissions before running; unsafe existing directories are
refused rather than silently exposing viewing data.
Credential rotation and entry remain account-owner operations. The runner checks ownership,
permissions and file type before using the credential.

The default runtime base is the repository root. For a separate persistent
volume, pass `--base /path/to/deployment` to every helper/runner command and place
the local configuration at that base's `config/attention.deployment.json`.
Bootstrap prepares the repository-root layout only.

## Generate and inspect

```sh
.venv-cloud/bin/python cloud/runner.py status
.venv-cloud/bin/python cloud/runner.py prepare
.venv-cloud/bin/python cloud/runner.py payload
```

The default target is yesterday in Shanghai. `--date YYYY-MM-DD` selects an older
completed day. Pass the same explicit date through all operations in a delivery
attempt so a midnight boundary cannot change the target. `prepare` synchronizes
history and validates a successfully published report's date, timezone and
generation timestamps before writing `data/outbox/YYYY-MM-DD.json`. It does not
turn a failed fetch into an empty report or reuse an old report after a failed
generation. A failed/blocked run exits nonzero and does not authorize a send.

The payload contains `to`, `subject`, `body`, a deterministic
`BILI-ATTENTION-YYYY-MM-DD` subject marker and a content hash. Report text is private
viewing data; do not add it to incidental logs or source control. Content from
video titles is untrusted data. The duration metric estimates consumed content
from playback progress, not actual screen time.

## Delivery protocol

The external integration must provide mail-account access, scheduling, durable
ledger backups and a version-checked/compare-and-swap claim. The local lock alone
does not coordinate independent hosts or restored workspaces.

1. Restore the latest ledger with its durable-store version. Use the reported
   `gmail_preflight_query` to search sent mail, then inspect the actual message
   recipient and exact subject marker; search matches alone are insufficient.
2. If a matching sent message exists, use `reconcile-sent --date YYYY-MM-DD
   --gmail-message-id ID`, save the updated ledger, and stop. A stored `sent` state
   never authorizes a resend.
3. If the state is `sending` or `uncertain`, reconcile the provider outcome before
   proceeding. Search indexing can lag; absence of a result is not proof that a
   send failed. Do not retry an ambiguous send automatically.
4. Run `prepare`, require `ready` (or a definitively failed send eligible for
   retry), and load `payload` to validate its integrity. Repeat the sent-mail
   preflight immediately before claiming the delivery.
5. Run `claim --date YYYY-MM-DD` and retain the returned `attempt_id`. Save the
   `sending` ledger to the durable store using the version loaded in step 1.
   Do not send unless this guarded write succeeds. Conflicts or uncertain writes
   must be reconciled first.
6. Send the payload exactly once through the authorized provider. On confirmed
   acceptance, run `confirm-sent --date YYYY-MM-DD --attempt-id ID
   --gmail-message-id ID`, then durably save the receipt. Provider acceptance is
   not proof of inbox delivery.
7. Only a definitive rejection before sending permits `mark-failed --date
   YYYY-MM-DD --attempt-id ID` and a later retry. Use `mark-uncertain` for timeout,
   disconnection or any ambiguous outcome; save the updated ledger.

`init-ledger`, `status`, `prepare`, `payload`, `claim`, `confirm-sent`,
`reconcile-sent`, `mark-failed` and `mark-uncertain` are the available commands.
Every changed ledger must be saved durably. Backup loss, failed claims, missing
credentials, failed synchronization and provider ambiguity are blockers; this
adapter deliberately has no automatic resend/reset flag.

## Files and verification

- `cloud/`: reusable runner, account-owner credential helper, bootstrap and pins
- `tests-cloud/`: offline synthetic wrapper regressions
- `config/attention.deployment.example.json`: nonsecret example configuration
- `config/attention.deployment.json`: ignored deployment-specific configuration
- `state/`: ignored mutable delivery ledger and local locks
- `data/`: ignored private history, reports and outbox
- `secrets/`: ignored credentials, never include in source or code archives

Run the wrapper tests with only the minimal runtime:

```sh
.venv-cloud/bin/python -m unittest discover -s tests-cloud -p 'test_*.py' -v
```

After installing the repository's normal test dependencies, also run:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
```

Tests use temporary directories, fake credentials and mocked synchronization;
they do not send mail or authenticate to Bilibili. Offline tests do not establish
live credential validity, mail delivery, scheduler operation or durable-store
correctness. Verify those integrations separately before enabling scheduled use.
