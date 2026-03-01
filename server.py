from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "update_config.json"
HISTORY_PATH = BASE_DIR / "update_history.json"
GITHUB_API_BASE = "https://api.github.com"


def default_channel(version: str, asset_name: str, asset_url: str, sha256: str) -> dict[str, Any]:
    return {
        "version": version,
        "mandatory": False,
        "asset": {
            "name": asset_name,
            "url": asset_url,
            "sha256": sha256,
        },
    }


def build_default_config() -> dict[str, Any]:
    return {
        "github_repo": "",
        "channels": {
            "stable": default_channel(
                version="1.0.0",
                asset_name="SoundboardEZ.exe",
                asset_url="https://yourcdn.com/1.0.0/SoundboardEZ.exe",
                sha256="PUT_REAL_HASH_HERE",
            ),
            "beta": default_channel(
                version="1.0.0-beta.1",
                asset_name="SoundboardEZ-beta.exe",
                asset_url="https://yourcdn.com/1.0.0-beta.1/SoundboardEZ-beta.exe",
                sha256="",
            ),
        },
    }


def build_default_history() -> dict[str, list[dict[str, Any]]]:
    return {"history": []}


def deep_copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def load_json(path: Path, default_payload: Any) -> Any:
    if not path.exists():
        save_json(path, default_payload)
        return deep_copy(default_payload)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        save_json(path, default_payload)
        return deep_copy(default_payload)


def save_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_channel(raw_channel: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    normalized = deep_copy(fallback)
    if not isinstance(raw_channel, dict):
        return normalized

    if "version" in raw_channel:
        normalized["version"] = str(raw_channel.get("version", "")).strip()
    normalized["mandatory"] = coerce_bool(raw_channel.get("mandatory", normalized["mandatory"]))

    raw_asset = raw_channel.get("asset", {})
    if isinstance(raw_asset, dict):
        if "name" in raw_asset:
            normalized["asset"]["name"] = str(raw_asset.get("name", "")).strip()
        if "url" in raw_asset:
            normalized["asset"]["url"] = str(raw_asset.get("url", "")).strip()
        if "sha256" in raw_asset:
            normalized["asset"]["sha256"] = str(raw_asset.get("sha256", "")).strip()

    return normalized


def normalize_config(raw_config: Any) -> dict[str, Any]:
    defaults = build_default_config()
    if not isinstance(raw_config, dict):
        return defaults

    raw_channels = raw_config.get("channels", {})
    if not isinstance(raw_channels, dict):
        raw_channels = {}

    return {
        "github_repo": str(raw_config.get("github_repo", defaults["github_repo"])).strip(),
        "channels": {
            "stable": normalize_channel(raw_channels.get("stable"), defaults["channels"]["stable"]),
            "beta": normalize_channel(raw_channels.get("beta"), defaults["channels"]["beta"]),
        },
    }


def load_config() -> dict[str, Any]:
    return normalize_config(load_json(CONFIG_PATH, build_default_config()))


def save_config(config: dict[str, Any]) -> None:
    save_json(CONFIG_PATH, normalize_config(config))


def normalize_history(raw_history: Any) -> dict[str, list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    source: Any = raw_history

    if isinstance(raw_history, dict):
        source = raw_history.get("history", [])

    if isinstance(source, list):
        for entry in source:
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    "timestamp": str(entry.get("timestamp", "")).strip(),
                    "channel": str(entry.get("channel", "")).strip(),
                    "old_version": str(entry.get("old_version", "")).strip(),
                    "new_version": str(entry.get("new_version", "")).strip(),
                    "mandatory": coerce_bool(entry.get("mandatory", False)),
                }
            )

    return {"history": entries}


def load_history() -> dict[str, list[dict[str, Any]]]:
    return normalize_history(load_json(HISTORY_PATH, build_default_history()))


def save_history(history: dict[str, list[dict[str, Any]]]) -> None:
    save_json(HISTORY_PATH, normalize_history(history))


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def append_history_entry(channel: str, old_version: str, new_version: str, mandatory: bool) -> None:
    history = load_history()
    history["history"].append(
        {
            "timestamp": utc_timestamp(),
            "channel": channel,
            "old_version": old_version,
            "new_version": new_version,
            "mandatory": mandatory,
        }
    )
    save_history(history)


def build_manifest(channel_config: dict[str, Any]) -> dict[str, Any]:
    asset = channel_config.get("asset", {})
    asset_name = str(asset.get("name", "SoundboardEZ.exe")).strip() or "SoundboardEZ.exe"
    return {
        "version": str(channel_config.get("version", "")).strip(),
        "mandatory": coerce_bool(channel_config.get("mandatory", False)),
        "files": {
            asset_name: {
                "url": str(asset.get("url", "")).strip(),
                "sha256": str(asset.get("sha256", "")).strip(),
            }
        },
    }


def admin_key() -> str:
    return os.environ.get("ADMIN_KEY", "").strip()


def provided_admin_key() -> str:
    return (request.args.get("key") or "").strip()


@app.before_request
def protect_admin_routes() -> None:
    if not request.path.startswith("/admin"):
        return

    expected_key = admin_key()
    if not expected_key:
        abort(500, description="ADMIN_KEY environment variable is not configured.")

    if provided_admin_key() != expected_key:
        abort(403, description="Forbidden. Access requires ?key=ADMIN_KEY.")


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "soundboardez-manifest-admin",
    }
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    return headers


def fetch_releases(repo: str) -> list[dict[str, Any]]:
    response = requests.get(
        f"{GITHUB_API_BASE}/repos/{repo}/releases",
        headers=github_headers(),
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    releases: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return releases

    for release in payload:
        if not isinstance(release, dict):
            continue
        releases.append(
            {
                "tag_name": str(release.get("tag_name", "")).strip(),
                "name": str(release.get("name") or release.get("tag_name") or "").strip(),
                "draft": coerce_bool(release.get("draft", False)),
                "prerelease": coerce_bool(release.get("prerelease", False)),
            }
        )
    return releases


def fetch_assets_for_tag(repo: str, tag: str) -> list[dict[str, str]]:
    safe_tag = quote(tag, safe="")
    response = requests.get(
        f"{GITHUB_API_BASE}/repos/{repo}/releases/tags/{safe_tag}",
        headers=github_headers(),
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    assets: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return assets

    for asset in payload.get("assets", []):
        if not isinstance(asset, dict):
            continue
        assets.append(
            {
                "name": str(asset.get("name", "")).strip(),
                "url": str(asset.get("browser_download_url", "")).strip(),
            }
        )
    return assets


def compute_sha256_from_url(file_url: str) -> str:
    digest = hashlib.sha256()
    with requests.get(file_url, stream=True, timeout=120) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                digest.update(chunk)
    return digest.hexdigest()


@app.route("/manifest")
def manifest() -> Any:
    config = load_config()
    selected_channel = (request.args.get("channel") or "stable").strip().lower()
    channel_key = "beta" if selected_channel == "beta" else "stable"
    return jsonify(build_manifest(config["channels"][channel_key]))


@app.route("/admin")
def admin_panel() -> Any:
    config = load_config()
    history = load_history()["history"]
    history.sort(key=lambda entry: entry.get("timestamp", ""), reverse=True)
    return render_template("admin.html", config=config, history=history, admin_key=provided_admin_key())


@app.post("/admin/update/<channel>")
def update_channel(channel: str) -> Any:
    channel_key = channel.strip().lower()
    if channel_key not in {"stable", "beta"}:
        abort(404)

    config = load_config()
    channel_config = config["channels"][channel_key]

    old_version = str(channel_config.get("version", "")).strip()
    new_version = (request.form.get("version") or old_version).strip()
    mandatory = coerce_bool(request.form.get("mandatory"))

    asset_name = (request.form.get("asset_name") or channel_config["asset"].get("name", "")).strip()
    asset_url = (request.form.get("asset_url") or channel_config["asset"].get("url", "")).strip()
    asset_sha256 = (request.form.get("asset_sha256") or channel_config["asset"].get("sha256", "")).strip()

    github_repo = (request.form.get("github_repo") or config.get("github_repo", "")).strip()
    config["github_repo"] = github_repo

    config["channels"][channel_key] = {
        "version": new_version,
        "mandatory": mandatory,
        "asset": {
            "name": asset_name,
            "url": asset_url,
            "sha256": asset_sha256,
        },
    }

    save_config(config)
    append_history_entry(channel_key, old_version, new_version, mandatory)

    return redirect(url_for("admin_panel", key=provided_admin_key()))


@app.get("/admin/api/releases")
def admin_api_releases() -> Any:
    config = load_config()
    repo = (request.args.get("repo") or config.get("github_repo", "")).strip()
    if not repo:
        return jsonify({"releases": []})

    try:
        releases = fetch_releases(repo)
    except requests.RequestException as exc:
        return jsonify({"error": "Failed to fetch releases from GitHub.", "details": str(exc)}), 502

    return jsonify({"releases": releases})


@app.get("/admin/api/assets")
def admin_api_assets() -> Any:
    config = load_config()
    repo = (request.args.get("repo") or config.get("github_repo", "")).strip()
    tag = (request.args.get("tag") or "").strip()

    if not repo:
        return jsonify({"error": "GitHub repository is required."}), 400
    if not tag:
        return jsonify({"error": "Release tag is required."}), 400

    try:
        assets = fetch_assets_for_tag(repo, tag)
    except requests.RequestException as exc:
        return jsonify({"error": "Failed to fetch release assets from GitHub.", "details": str(exc)}), 502

    return jsonify({"tag": tag, "assets": assets})


@app.post("/admin/api/sha256")
def admin_api_sha256() -> Any:
    payload = request.get_json(silent=True) or {}
    source_url = (payload.get("url") or request.form.get("url") or "").strip()
    if not source_url:
        return jsonify({"error": "Asset URL is required."}), 400

    try:
        hash_value = compute_sha256_from_url(source_url)
    except requests.RequestException as exc:
        return jsonify({"error": "Failed to download asset for hashing.", "details": str(exc)}), 502

    return jsonify({"sha256": hash_value})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
