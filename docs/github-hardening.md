# GitHub security controls

Date: 2026-11-14

This record documents the approved credential-theft/CI-injection hardening. It is not evidence that an account was compromised. Changes preserve the existing application runtime.

## Verified server changes

Branch protection is pending plan availability; this change does not claim that production is protected.

## Workflow changes and verification

External actions use full commit SHAs. Read access is explicit at workflow level; checkout credentials are not persisted in build/test jobs. Existing publication jobs retain only their required exceptions. Tags were resolved through the upstream GitHub API; a SHA prevents tag movement but does not make arbitrary repository code trustworthy.

Validation: actionlint 1.7.12, exit 0. Shellcheck and pyflakes were not run. Application builds/tests were not run for changes limited to workflow permissions/action references. Production was not exercised.

- `.github/workflows/profile.yml`

The generator now runs with read permissions and no persisted checkout token. A separate job transfers the generated artifact and publishes it without running repository scripts. The publisher still needs contents: write and persists checkout credentials. Requiring PRs on main is deferred until this automated publisher has an approved PR or separate-branch flow; no administrator bypass is added.

## Limits and rollback

Write credentials, administrator access and production integrations still require independent review. Re-enable Actions only after reviewing every branch and documenting a need for CI. Restore previous controls only through an approved change; revert code through a PR, never a history rewrite.

[Central policy and audit record](https://github.com/notolac/home-lab/blob/main/_shared/orquestacion/github-hardening.md).
