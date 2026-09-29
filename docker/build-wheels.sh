#!/usr/bin/env bash
# Build the two ORM wheels the image installs, into wheels/.
#
# They exist because PyPI lags the release branches by one version: the project
# declares `rhosocial-activerecord>=1.0.0.dev0`, so a plain `pip install .` takes
# dev29 / dev16 while the venv being developed against is an editable checkout of
# dev30 / dev17. The image would then run different ORM code than the tests.
#
# The build is run against a copy on a native filesystem on purpose. Both ORM
# repositories sit on a Windows drive mount here, where setuptools' package
# discovery stats thousands of files and takes minutes; on /tmp the same build is
# seconds. Nothing is modified in the source trees.
set -euo pipefail

REPOS="${RHOSOCIAL_REPOS:-/mnt/i/GitHubRepositories/rhosocial}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEEL_DIR="$PROJECT_ROOT/wheels"
STAGE="${TMPDIR:-/tmp}/schedule-manager-wheels.$$"
INDEX="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"

PACKAGES=(python-activerecord python-activerecord-postgres)

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

mkdir -p "$WHEEL_DIR" "$STAGE"

for package in "${PACKAGES[@]}"; do
    source_dir="$REPOS/$package"
    if [[ ! -d "$source_dir" ]]; then
        echo "missing checkout: $source_dir" >&2
        echo "set RHOSOCIAL_REPOS to the directory holding them" >&2
        exit 1
    fi
    mkdir -p "$STAGE/$package"
    for item in pyproject.toml README.md LICENSE MANIFEST.in CHANGELOG.md src; do
        [[ -e "$source_dir/$item" ]] && cp -a "$source_dir/$item" "$STAGE/$package/"
    done
    printf 'staged %-30s %s files\n' "$package" "$(find "$STAGE/$package" -type f | wc -l)"
done

# --no-deps: only these two packages are being pinned. Their own dependencies are
# still resolved normally by the image build.
python -m pip wheel --no-deps --index-url "$INDEX" --wheel-dir "$WHEEL_DIR" \
    "${STAGE}/python-activerecord" "${STAGE}/python-activerecord-postgres"

echo
echo "wheels for the image:"
ls -1 "$WHEEL_DIR"
