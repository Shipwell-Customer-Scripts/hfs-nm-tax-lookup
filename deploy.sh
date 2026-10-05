#!/bin/bash
# Deploy hfs-nm-tax-lookup Lambda.
#   ./deploy.sh --build-only   build + import-check, do not upload
#   ./deploy.sh                build, import-check, upload
# Source of truth for shared modules is ../hfs-shared. Lambda-local .py files NEVER overwrite a
# same-named hfs-shared module (warning printed, local copy ignored). The build dir is created fresh
# every run, so stale/nested package_build/ content cannot ship.
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HFS_SHARED="$SCRIPT_DIR/../hfs-shared"
CREATE_DIR="$SCRIPT_DIR/../hfs-create-shipment-from-order"
DEPS_DIR="$SCRIPT_DIR/package"          # pip-installed dependencies (third-party only matter)
BUILD="$SCRIPT_DIR/package_build"
ZIP="$SCRIPT_DIR/deploy.zip"

rm -rf "$BUILD" "$ZIP"
mkdir -p "$BUILD"
# 1) third-party deps from package/ (skip any top-level .py: those are stale copies of our own modules,
#    except pure-python third-party single-file modules listed below)
( cd "$DEPS_DIR" && tar cf - --no-wildcards-match-slash --exclude='__pycache__' --exclude='*.pyc' --exclude='package_build' --exclude='*.zip' \
    --exclude='./*.py' . ) | ( cd "$BUILD" && tar xf - )
for keep in six.py google_auth_httplib2.py; do [ -f "$DEPS_DIR/$keep" ] && cp "$DEPS_DIR/$keep" "$BUILD/$keep"; done

# 2) lambda-local source (skip anything that shadows hfs-shared)
for f in "$SCRIPT_DIR"/*.py; do
  n=$(basename "$f")
  if [ -f "$HFS_SHARED/$n" ]; then
    echo "WARNING: $n exists locally AND in hfs-shared - IGNORING local copy, using hfs-shared. Delete it." >&2
    continue
  fi
  cp "$f" "$BUILD/$n"
done

# 3) hfs-shared LAST (always wins)
cp "$HFS_SHARED"/*.py "$BUILD/"

# 4) modules hfs-shared/carrier_assignment.py imports that live only in the create lambda
for m in hac_capacity.py google_sheets_client.py; do cp "$CREATE_DIR/$m" "$BUILD/$m"; done

# 5) import smoke test (catches ModuleNotFoundError before upload)
[ -f "$BUILD/requests/adapters.py" ] || { echo "requests/adapters.py missing from build - not deploying" >&2; exit 1; }
( cd "$BUILD" && python3 -S -c "import sys; sys.path.insert(0,'.'); import handler" ) || { echo "import check FAILED - not deploying" >&2; exit 1; }

cd "$BUILD"
zip -r "$ZIP" . --exclude "googleapiclient/discovery_cache/documents/*" --exclude "*/__pycache__/*" --exclude "*.pyc" --exclude "*.zip" -q
cd "$SCRIPT_DIR"
echo "Built $ZIP ($(du -h "$ZIP" | cut -f1))"
[ "$1" = "--build-only" ] && exit 0
aws lambda update-function-code --function-name hfs-nm-tax-lookup --zip-file fileb://deploy.zip --region us-west-2 --query 'LastModified' --output text
echo "Deployed."
