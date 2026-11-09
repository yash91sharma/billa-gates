# Deployment and development

The [README](README.md) covers running the published image. This file covers building your own,
releasing, and working on the code. Architecture and invariants are in [CLAUDE.md](CLAUDE.md).

## Build the image

```bash
docker build --build-arg RESTIC_ARCH=arm64 -t billa-gates:latest .   # Apple Silicon / ARM64 Linux
docker build --build-arg RESTIC_ARCH=amd64 -t billa-gates:latest .   # Intel / AMD
```

- `RESTIC_ARCH` is required (`arm64` or `amd64`); the build fails without it. One build produces
  one architecture.
- `RESTIC_VERSION` defaults to the `ARG` in the `Dockerfile`. 0.19.0 is a floor, not a
  suggestion: the `fastest` and `better` compression modes offered in the UI do not exist before
  it. The download is verified against restic's `SHA256SUMS`.

## Release

`.github/workflows/docker-publish.yml` builds and pushes to Docker Hub when a `v*` tag is pushed,
then publishes a GitHub release for that tag. Branch pushes publish nothing.

```bash
cd frontend && npm version patch   # or minor / major — bumps package.json, shown in the sidebar
# commit, then:
git tag vX.Y.Z && git push origin vX.Y.Z
```

The release notes are a `docker pull` line for the image and the commits since the previous `v*`
tag. An annotated tag (`git tag -a vX.Y.Z -m "..."`) also works, and its message leads the notes. The release is created only after the image push
succeeds. If that step fails, re-run the workflow, or delete the half-made release first if one
exists, since `gh release create` refuses a tag that already has one.

### Bumping restic

Change `ARG RESTIC_VERSION` in the `Dockerfile`. Image needs to be rebuilt for this to take effect.
