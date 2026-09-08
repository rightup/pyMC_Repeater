#!/usr/bin/env bash
# Validate stable publication before Docker login/build. Dev remains push-driven.
set -euo pipefail

channel=""
case "${GITHUB_REF:?}" in
  refs/heads/dev) channel=dev ;;
  refs/heads/main|refs/tags/*)
    git fetch origin main
    git merge-base --is-ancestor HEAD FETCH_HEAD || {
      printf '%s\n' 'Release commit must belong to main.' >&2
      exit 1
    }
    if [[ "$GITHUB_REF" == refs/tags/* ]]; then
      tag="${GITHUB_REF#refs/tags/}"
    else
      tag="$(git describe --tags --exact-match HEAD)"
    fi
    [[ "$tag" =~ ^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
      printf '%s\n' 'Main publication requires an exact stable X.Y.Z or vX.Y.Z tag.' >&2
      exit 1
    }
    [[ "$(git rev-parse "refs/tags/${tag}^{commit}")" == "$(git rev-parse HEAD)" ]] || exit 1
    [[ "${PACKAGE_VERSION:?}" == "${tag#v}" ]] || {
      printf '%s\n' 'Computed package version does not match the release tag.' >&2
      exit 1
    }
    channel=main
    ;;
esac
printf 'channel=%s\n' "$channel" >> "${GITHUB_OUTPUT:?}"
