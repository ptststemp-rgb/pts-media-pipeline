# Two-account runner scheduling

Production dispatch alternates between the primary and secondary GitHub accounts. The application ceiling is 40 active runner jobs, with a hard limit of 20 per account owner. GitHub's own plan, account-wide concurrency, hosted-runner availability, billing and rate limits still apply; these limits are not per repository. Adding repositories does not increase an account's capacity.

The primary token/repository retain their existing settings/environment behavior. The secondary account is stored in the private SQLite setting `github_secondary_accounts`. Credentials are excluded from JSON job/account models and only masked keys are returned to administrator settings. Daily encrypted database backups include the secondary configuration. Repository secrets are separately provisioned with the existing worker credential and upload-origin configuration; private source URLs are never workflow inputs.

Each job stores its assigned `runner_repo`. Atomic database reservations enforce both ceilings and persist `pipeline_round_robin_next` in the same transaction. Alternation survives backend restarts. Existing active jobs retain their original account and attempt; legacy jobs are attributed to the primary repository during migration. Retries receive a new fenced attempt and may use another available account.

If the preferred account is full, disabled, rate-limited, rejects dispatch or has a network failure, the scheduler tries the other account. Failing accounts cool down before being tried again. If neither account can accept a job, it remains queued rather than failing solely because account capacity is unavailable. Uncertain dispatch responses cannot replace a worker that has already started; superseded pending attempts are refused by the worker API.

The 40-job ceiling applies to GitHub downloading/encoding/uploading, not to heavy work on the VPS/Pi. Upload bodies remain streamed with 32 global chunk slots. Final upload assembly is serialized, free-space checks protect the VPS, and redundant chunk parts are removed only after a complete assembled file is committed to the job. Pi transfers remain limited to two, and movie-download preparation remains one at a time. No live movie encoding runs on either server.

Administrators can select Primary or Secondary under API Management to inspect the assigned repository, masked token and scheduler limits, or rotate a token with password re-verification and CSRF protection. The two roles must belong to different GitHub account owners. Updating one role does not reset the other or interrupt already-running worker attempts.

Tests simulate 80 concurrent submissions and verify exactly 40 dispatches split 20/20, persistent alternation, account failover, cooldown, legacy capacity accounting, credential masking, cancelled requests and uncertain-response attempt fencing. Deployment smoke checks use four disposable short clips across both accounts, not 40 full movie encodes.
