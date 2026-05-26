#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
#     "pyyaml>=6.0",
# ]
# ///
"""Post new snarkysyndo messages to Mastodon and Bluesky.

Scans ``messages/*.md`` for files where ``posted_at`` is null, posts the
body to each configured platform, and rewrites the frontmatter with the
resulting URLs (or error strings on failure). Idempotent — already-posted
messages are skipped, and a per-platform success is remembered so a later
retry only re-attempts the platform that previously failed.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

# AIDEV-NOTE: field order is enforced when rewriting frontmatter so diffs
# stay minimal. New fields go at the end of FIELD_ORDER.
FIELD_ORDER = (
    "id",
    "created_at",
    "posted_at",
    "mastodon_url",
    "bluesky_url",
    "mastodon_error",
    "bluesky_error",
)
MESSAGES_DIR = Path("messages")
BLUESKY_PDS = "https://bsky.social"
MAX_ERROR_LEN = 500


def parse_message(text: str) -> tuple[dict[str, Any], str]:
    """Split a message file into (frontmatter dict, body string)."""
    if not text.startswith("---\n"):
        raise ValueError("missing frontmatter opener")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("missing frontmatter closer")
    fm_raw = yaml.safe_load(text[4:end]) or {}
    if not isinstance(fm_raw, dict):
        raise ValueError("frontmatter must be a YAML mapping")
    body = text[end + len("\n---\n") :].strip()
    return fm_raw, body


def serialize_message(fm: dict[str, Any], body: str) -> str:
    """Render a message file with FIELD_ORDER-canonical frontmatter."""
    ordered: dict[str, Any] = {}
    for key in FIELD_ORDER:
        ordered[key] = fm.get(key)
    for key, value in fm.items():
        if key not in ordered:
            ordered[key] = value
    fm_text = yaml.safe_dump(
        ordered,
        sort_keys=False,
        allow_unicode=True,
        width=2**31,
    )
    return f"---\n{fm_text}---\n{body}\n"


def now_utc_iso() -> str:
    """Seconds-precision UTC ISO timestamp with trailing 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = " ".join(text.split())
    if len(text) > MAX_ERROR_LEN:
        text = text[: MAX_ERROR_LEN - 3] + "..."
    return text


def _check_response(resp: httpx.Response, platform: str) -> None:
    if resp.status_code >= 400:
        try:
            detail: Any = resp.json()
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(f"{platform} HTTP {resp.status_code}: {detail}")


def normalize_mastodon_instance(value: str) -> str:
    """Validate MASTODON_INSTANCE and return a bare ``host[:port]``.

    Accepts either a bare hostname (``mastodon.social``) or a full
    ``https://host`` URL. Rejects ``http://`` (would send the bearer token
    in plaintext), embedded credentials, query/fragment, and any path
    other than ``/`` — the README documents host-only.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("MASTODON_INSTANCE is empty")
    if raw.lower().startswith("http://"):
        raise ValueError("MASTODON_INSTANCE must use https, not http")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme != "https":
        raise ValueError(
            f"MASTODON_INSTANCE scheme must be https, got {parsed.scheme!r}"
        )
    if parsed.username or parsed.password:
        raise ValueError("MASTODON_INSTANCE must not contain credentials")
    if parsed.path not in ("", "/"):
        raise ValueError(
            f"MASTODON_INSTANCE must be host-only, got path {parsed.path!r}"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("MASTODON_INSTANCE must not have query or fragment")
    if not parsed.hostname:
        raise ValueError("MASTODON_INSTANCE has no hostname")
    return parsed.netloc


def post_to_mastodon(*, instance: str, token: str, body: str) -> str:
    # ``instance`` here is already validated (see normalize_mastodon_instance);
    # the scheme is fixed to https so the token never leaves a TLS connection.
    resp = httpx.post(
        f"https://{instance}/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": body},
        timeout=30.0,
    )
    _check_response(resp, "mastodon")
    data = resp.json()
    url = data.get("url")
    if not url:
        raise RuntimeError(f"mastodon response missing 'url': {data!r}")
    return str(url)


def post_to_bluesky(
    *, handle: str, app_password: str, body: str, created_at: str
) -> str:
    with httpx.Client(base_url=BLUESKY_PDS, timeout=30.0) as client:
        sess = client.post(
            "/xrpc/com.atproto.server.createSession",
            json={"identifier": handle, "password": app_password},
        )
        _check_response(sess, "bluesky.session")
        sess_data = sess.json()
        access_jwt = sess_data["accessJwt"]
        did = sess_data["did"]

        rec = client.post(
            "/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {access_jwt}"},
            json={
                "repo": did,
                "collection": "app.bsky.feed.post",
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": body,
                    "createdAt": created_at,
                },
            },
        )
        _check_response(rec, "bluesky.create")
        rec_data = rec.json()
        # AT URI is at://{did}/{collection}/{rkey} — bsky.app accepts handle
        # in place of did for the public web URL.
        rkey = rec_data["uri"].rsplit("/", 1)[-1]
        return f"https://bsky.app/profile/{handle}/post/{rkey}"


def main() -> int:
    mastodon_instance = os.environ.get("MASTODON_INSTANCE", "").strip()
    mastodon_token = os.environ.get("MASTODON_TOKEN", "").strip()
    bluesky_handle = os.environ.get("BLUESKY_HANDLE", "").strip()
    bluesky_password = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()

    # AIDEV-NOTE: per-platform env var pairs, used both for the have_*
    # booleans and for diagnostics that name the exact missing variables.
    platform_vars: tuple[tuple[str, tuple[str, str], tuple[str, str]], ...] = (
        (
            "mastodon",
            ("MASTODON_INSTANCE", "MASTODON_TOKEN"),
            (mastodon_instance, mastodon_token),
        ),
        (
            "bluesky",
            ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"),
            (bluesky_handle, bluesky_password),
        ),
    )

    have_mastodon = bool(mastodon_instance and mastodon_token)
    have_bluesky = bool(bluesky_handle and bluesky_password)

    # Warn on partial config (one of the pair set, the other empty) so a
    # typo like MASTODON_INSTACE doesn't silently disable a platform.
    for name, var_names, values in platform_vars:
        set_vars = [n for n, v in zip(var_names, values) if v]
        missing_vars = [n for n, v in zip(var_names, values) if not v]
        if set_vars and missing_vars:
            print(
                f"WARNING: {name} is partially configured — disabled. "
                f"Set: {', '.join(set_vars)}. "
                f"Missing or empty: {', '.join(missing_vars)}.",
                file=sys.stderr,
            )

    if have_mastodon:
        try:
            mastodon_instance = normalize_mastodon_instance(mastodon_instance)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    if not (have_mastodon or have_bluesky):
        print("ERROR: no platform credentials configured.", file=sys.stderr)
        for name, var_names, values in platform_vars:
            status = [
                f"{n}={'set' if v else 'missing/empty'}"
                for n, v in zip(var_names, values)
            ]
            print(f"  {name}: {', '.join(status)}", file=sys.stderr)
        print(
            "Set both env vars for at least one platform "
            "(MASTODON_INSTANCE+MASTODON_TOKEN or "
            "BLUESKY_HANDLE+BLUESKY_APP_PASSWORD).",
            file=sys.stderr,
        )
        return 2

    if not MESSAGES_DIR.is_dir():
        print(f"ERROR: messages dir {MESSAGES_DIR} not found", file=sys.stderr)
        return 2

    files = sorted(MESSAGES_DIR.glob("*.md"))
    if not files:
        print("no messages found")
        return 0

    posted = partial = skipped = 0

    for path in files:
        try:
            fm, body = parse_message(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"SKIP {path.name}: {exc}")
            skipped += 1
            continue

        if fm.get("posted_at") is not None:
            skipped += 1
            continue

        if not body:
            print(f"SKIP {path.name}: empty body")
            skipped += 1
            continue

        created_at = fm.get("created_at") or now_utc_iso()
        attempted: list[str] = []
        succeeded: list[str] = []
        changed = False

        if have_mastodon and fm.get("mastodon_url") is None:
            attempted.append("mastodon")
            try:
                fm["mastodon_url"] = post_to_mastodon(
                    instance=mastodon_instance,
                    token=mastodon_token,
                    body=body,
                )
                fm["mastodon_error"] = None
                succeeded.append("mastodon")
            except Exception as exc:  # noqa: BLE001 — capture all for retry
                fm["mastodon_error"] = short_error(exc)
            changed = True

        if have_bluesky and fm.get("bluesky_url") is None:
            attempted.append("bluesky")
            try:
                fm["bluesky_url"] = post_to_bluesky(
                    handle=bluesky_handle,
                    app_password=bluesky_password,
                    body=body,
                    created_at=str(created_at),
                )
                fm["bluesky_error"] = None
                succeeded.append("bluesky")
            except Exception as exc:  # noqa: BLE001
                fm["bluesky_error"] = short_error(exc)
            changed = True

        # Mark fully posted only when every *configured* platform has a URL.
        all_done = (not have_mastodon or fm.get("mastodon_url") is not None) and (
            not have_bluesky or fm.get("bluesky_url") is not None
        )
        if all_done and fm.get("posted_at") is None:
            fm["posted_at"] = now_utc_iso()
            changed = True

        if changed:
            path.write_text(serialize_message(fm, body), encoding="utf-8")

        if attempted:
            ok = set(attempted) == set(succeeded)
            label = "OK" if ok else "PARTIAL"
            print(
                f"{label} {path.name}: "
                f"attempted={','.join(attempted)} "
                f"succeeded={','.join(succeeded) or 'none'}"
            )
            if ok:
                posted += 1
            else:
                partial += 1
        else:
            skipped += 1

    print(f"summary: posted={posted} partial={partial} skipped={skipped}")
    # Partial failures are not a hard error — we want the workflow to commit
    # the recorded errors so a follow-up run can retry the missing side.
    return 0


if __name__ == "__main__":
    sys.exit(main())
