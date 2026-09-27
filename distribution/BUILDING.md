# Windows release build

The release script produces a portable app directory and an Inno Setup installer. It uses only the sanitized example configuration; it does not package the root `config.json`, backups, logs, databases, or Git metadata.

## Requirements

- Windows 10/11 x64 and Python 3.12 x64.
- Inno Setup 7.1.0. Keep `ISCC.exe` at `distribution\tools\InnoSetup7\ISCC.exe`. This local tool directory is ignored by Git and is not included in the installer.
- Network access for installing the pinned Python build dependencies, unless their wheels are already cached.

## Prepare the isolated build environment

Run these commands from the repository root:

```powershell
py -3.12 -m venv .\distribution\build-env
.\distribution\build-env\Scripts\python.exe -m pip install --upgrade pip
.\distribution\build-env\Scripts\python.exe -m pip install -r .\distribution\requirements-build.lock.txt
.\distribution\build-env\Scripts\python.exe -m pip install --no-deps .\vendor\wechatauto-replica
```

The lock file pins the Python build environment. The final command installs the vendored dependency as a regular package in that environment; the release script refreshes it from the checked-in source snapshot before building.

## Build

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\distribution\build_release.ps1
```

The version is read from `distribution\Version.iss`. Each build writes to `distribution\output\<version>` and `distribution\release\<version>`. The script refuses to overwrite an existing version; increment `AppVersion` in `Version.iss` for another build.

Before publishing a release, check the installer contents and SHA-256, and install it on a clean Windows machine. Upload the `.exe` as a GitHub Release asset; do not commit generated build folders or the installer to the source repository.
