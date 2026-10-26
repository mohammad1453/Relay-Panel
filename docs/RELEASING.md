# Versions and releases

Relay uses [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

| Change | Bump | Example |
|---|---|---|
| Bug fixes only, nothing to do for operators | PATCH | 1.0.0 → 1.0.1 |
| New features, existing installs keep working (also new database migrations that run by themselves) | MINOR | 1.0.1 → 1.1.0 |
| Operators must change something by hand, a setting is removed or renamed, or a supported 3X-UI version is dropped | MAJOR | 1.1.0 → 2.0.0 |

The number lives in one place: the `VERSION` file. The panel, `/healthz`, the
release archive name and the online installer all read it.

## Making a release

1. Add your changes under `## [Unreleased]` in `CHANGELOG.md` while you work.
2. When ready, pick the new number and rename that section to
   `## [X.Y.Z] - YYYY-MM-DD`, add a new empty `## [Unreleased]` above it, and
   update the links at the bottom.
3. Write the same number in `VERSION`.
4. Run the tests: `python -m pytest -q`.
5. Merge to `main`, then tag that commit and push the tag:

   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "Relay vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. The **Release** GitHub Action checks that the tag matches `VERSION`, runs
   the tests, builds `relay-panel-vX.Y.Z.tar.gz` with its `.sha256`, and
   publishes a GitHub release with the notes from the changelog.

From then on the online installer installs or updates to the new version:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mohammad1453/Relay-Panel/main/install-online.sh)
```

Never move or reuse a published tag. If a release is broken, publish a new
PATCH version.
