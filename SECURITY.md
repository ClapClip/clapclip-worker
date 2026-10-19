# Security and source releases

## Reporting a vulnerability

Use the repository's [private vulnerability report form](https://github.com/ClapClip/clapclip-worker/security/advisories/new)
when private reporting is enabled. If it is unavailable, open an issue containing
only "Please provide a private security contact"; do not include exploit details,
credentials, signed media URLs or user images in that issue. We do not publish
a guaranteed response time. Security fixes target the current `main` version;
older deployment snapshots must be reviewed and updated by their operator.

## Local data and deployment boundaries

The workbench binds to loopback and does not use the private queue by default.
It is not an Internet-facing multi-user service. Distributed deployments receive
media and presigned storage URLs from their own backend; their operators are
responsible for account authorization, storage access and retention. Model and
dependency licenses remain separate; see [THIRD_PARTY.md](THIRD_PARTY.md).

## Source releases

Keep `.env*` files (except the two committed examples), token caches, private
keys, models, user media, task directories and logs out of public releases.
Git ignores do not protect a manually created archive of the working folder.

Never upload or zip the entire checkout: ignored `build/` directories may hold
real deployment credentials and logs. Create source archives from a reviewed
commit instead, using `git archive --format=zip --output=source.zip HEAD`.
Review `git ls-files` and every commit to be published first. `MANIFEST.in`
also excludes private configuration, runtime folders, model weights, local
databases and keys from Python source distributions. Only the two sanitized
environment examples are included. Recheck the actual archive contents before
uploading; ignore rules do not prevent an explicit forced Git add.

Configure your own compatible Worker API, Cognito user pool and app client,
Worker account and optional model release repository. Example domains and
accounts are placeholders. Public source code does not grant access to any
hosted service or private model assets. Use read-only repository access for
model downloads; public repositories do not require a GitHub token.

CLI log formatters redact HTTP(S) URLs, bearer/basic credentials and common
secret assignments, including exception chains. Public failure messages are
redacted before being sent to the API. Treat logs as private regardless:
diagnostics can still include hostnames, task IDs and local file paths.
Previously generated logs are not retroactively cleaned by a code update.

Before publishing, scan the exact release files and any Git history, tags,
release attachments and repository metadata that will become public. Git
author emails are part of history. A source-only archive can omit that history
without rewriting or force-pushing the production repository. Rotate any real
credential that has already been exposed; deleting a file is not revocation.

Review the license for this project's source and the licenses of each model
and third-party dependency before redistribution. Model weights are not
included in the source release.
