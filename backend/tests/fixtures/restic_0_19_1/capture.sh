#!/bin/bash
# Regenerate the recorded restic output in this directory.
#
# Run it on a restic version bump, then run tests/test_restic_contract.py: what
# fails there is what the new version actually changed for billa-gates. Commit
# the regenerated files together with the Dockerfile's RESTIC_VERSION change —
# the contract test asserts the two agree.
#
# Must run as root inside a Linux container (it needs to chown for the
# permission-denied capture, which requires a non-root uid to reproduce), and it
# writes to /sources, /destinations and /tmp. Nothing here is a test dependency
# at runtime: the suite reads only the recorded files.
#
#   RESTIC=/path/to/restic bash capture.sh && cp /tmp/captured/* .
#
# Paths are made to look production-like (/sources/documents,
# /destinations/main) so the recorded fixtures read like real runs. The password
# below is a throwaway used only to create the temporary repositories; it must
# never appear in a captured file (the capture step verifies this).
set -u
R="${RESTIC:-/tmp/restic19}"
OUT=/tmp/captured
rm -rf "$OUT" /sources /destinations /tmp/rcache; mkdir -p "$OUT" /tmp/rcache
mkdir -p /sources/documents/reports /destinations/main
# Throwaway password for the ephemeral repos this script creates under
# /destinations. Not a credential — it never appears in any captured output.
export RESTIC_PASSWORD="fixture-capture-throwaway"
export RESTIC_REPOSITORY="/destinations/main/Documents"
export RESTIC_CACHE_DIR=/tmp/rcache

echo hello > /sources/documents/notes.txt
head -c 400000 /dev/urandom > /sources/documents/blob.bin
echo report > /sources/documents/reports/q1.txt

"$R" version --json > "$OUT/version.json" 2>/dev/null
"$R" version > "$OUT/version.txt" 2>/dev/null
"$R" init >/dev/null 2>&1
"$R" cat config > "$OUT/cat_config.json" 2>/dev/null

# --- rc=0 backup: summary + status lines -------------------------------------
"$R" backup --host billa-gates --tag daily --tag important --json /sources/documents \
  > "$OUT/backup_rc0.stdout" 2> "$OUT/backup_rc0.stderr"
echo "rc0=$?" >> "$OUT/exit_codes.txt"

# A status line needs a backup long enough to emit one.
head -c 600000000 /dev/urandom > /sources/documents/big.bin
"$R" backup --host billa-gates --json /sources/documents \
  > "$OUT/backup_progress.stdout" 2>/dev/null
grep -m1 '"message_type":"status"' "$OUT/backup_progress.stdout" > "$OUT/backup_status_line.json"
rm -f /sources/documents/big.bin

# --- snapshots --json --------------------------------------------------------
"$R" snapshots --json --no-lock > "$OUT/snapshots.json" 2>/dev/null
"$R" snapshots --latest 1 --json --no-lock > "$OUT/snapshots_latest.json" 2>/dev/null

# --- rc=3 partial backup (unreadable subdir, run as nobody) ------------------
mkdir -p /sources/partial/ok /sources/partial/secret
echo v > /sources/partial/ok/f.txt
echo s > /sources/partial/secret/hidden.txt
chmod 700 /sources/partial/secret
chmod -R a+rx /sources/partial/ok
chmod a+rx / /sources /sources/partial
mkdir -p /tmp/rcache2 /destinations/main/Partial
chown -R nobody:nogroup /tmp/rcache2
RESTIC_REPOSITORY=/destinations/main/Partial "$R" init >/dev/null 2>&1
chown -R nobody:nogroup /destinations/main/Partial
setpriv --reuid=65534 --regid=65534 --clear-groups \
  env RESTIC_REPOSITORY=/destinations/main/Partial RESTIC_PASSWORD="$RESTIC_PASSWORD" \
      RESTIC_CACHE_DIR=/tmp/rcache2 \
  "$R" backup --host billa-gates --json /sources/partial \
  > "$OUT/backup_rc3.stdout" 2> "$OUT/backup_rc3.stderr"
echo "rc3=$?" >> "$OUT/exit_codes.txt"

# --- failure exit codes ------------------------------------------------------
RESTIC_REPOSITORY=/destinations/main/Missing "$R" cat config \
  > /dev/null 2> "$OUT/cat_config_rc10.stderr"
echo "rc10=$?" >> "$OUT/exit_codes.txt"
RESTIC_PASSWORD=wrong "$R" cat config > /dev/null 2> "$OUT/cat_config_rc12.stderr"
echo "rc12=$?" >> "$OUT/exit_codes.txt"
"$R" backup --host billa-gates --json /sources/does-not-exist \
  > "$OUT/backup_missing_source.stdout" 2> "$OUT/backup_missing_source.stderr"
echo "missing_source=$?" >> "$OUT/exit_codes.txt"

# --- forget / check / prune / unlock ----------------------------------------
"$R" forget --group-by '' --keep-last 1 > "$OUT/forget.stdout" 2> "$OUT/forget.stderr"
echo "forget=$?" >> "$OUT/exit_codes.txt"
"$R" check > "$OUT/check.stdout" 2>&1; echo "check=$?" >> "$OUT/exit_codes.txt"
"$R" prune > "$OUT/prune.stdout" 2>&1; echo "prune=$?" >> "$OUT/exit_codes.txt"
"$R" unlock > "$OUT/unlock.stdout" 2>&1; echo "unlock=$?" >> "$OUT/exit_codes.txt"

echo "=== captured ==="; ls -la "$OUT"; cat "$OUT/exit_codes.txt"
