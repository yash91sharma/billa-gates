# Deployment

## Build the image

```bash
# Apple Silicon / ARM64 Linux:
docker build --build-arg RESTIC_ARCH=arm64 -t billa-gates:latest .

# Intel/AMD x86-64:
docker build --build-arg RESTIC_ARCH=amd64 -t billa-gates:latest .
```

`RESTIC_ARCH` is **required** — the build fails loudly without it. Only
`arm64` and `amd64` are accepted.

`RESTIC_VERSION` defaults to the version pinned in the `Dockerfile`.

## Version Bump

```bash
cd frontend

npm version patch    # 1.0.6 -> 1.0.7  (bug fixes)
npm version minor    # 1.0.6 -> 1.1.0  (new features, backwards-compatible)
npm version major    # 1.0.6 -> 2.0.0  (breaking changes)
```

### Manual

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

---