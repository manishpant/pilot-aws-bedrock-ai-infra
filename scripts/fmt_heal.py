#!/usr/bin/env python3
"""Send a terraform fmt failure to Bedrock and open a GitHub pull request."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "manishpant/pilot-aws-bedrock-ai-infra")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.amazon.nova-2-lite-v1:0")
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "eu-north-1"))
BASE_BRANCH = os.environ.get("PR_BASE_BRANCH", "main")


def repo_root() -> Path:
    here = Path.cwd()
    if (here / "versions.tf").is_file():
        return here
    return Path(__file__).resolve().parents[1]


ROOT = repo_root()


def run(
    args: list[str],
    check: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )


def redact(text: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and text:
        return text.replace(token, "***")
    return text


def command_text(result: subprocess.CompletedProcess[str]) -> str:
    return redact(((result.stdout or "") + "\n" + (result.stderr or "")).strip())


def terraform_fmt_diff() -> tuple[int, str]:
    result = run(["terraform", "fmt", "-check", "-recursive", "-diff"])
    return result.returncode, command_text(result)


def files_from_diff(diff_text: str) -> list[str]:
    found: list[str] = []
    for line in diff_text.splitlines():
        if not line or line[0] in {" ", "+", "-", "@", "\\"}:
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        candidate = line.strip().removeprefix("./")
        path = Path(candidate)
        if path.suffix not in {".tf", ".tfvars"} or path.is_absolute() or ".." in path.parts:
            continue
        if (ROOT / path).is_file() and candidate not in found:
            found.append(candidate)
    return found


def parse_model_json(text: str) -> dict:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Bedrock did not return JSON")
    data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        raise ValueError("Bedrock JSON must contain a summary")
    if not isinstance(data.get("files"), list) or not data["files"]:
        raise ValueError("Bedrock JSON must contain files")
    return data


def file_bundle(paths: list[str]) -> str:
    parts: list[str] = []
    for path in paths:
        parts.append(f"===== {path} =====\n{(ROOT / path).read_text()}")
    return "\n".join(parts)


def ask_bedrock(diff_text: str, paths: list[str]) -> dict:
    prompt = (
        "terraform fmt -check failed. Reformat only. Do not change resource names, "
        "arguments, or logic.\n"
        "Return one JSON object and no markdown with this shape:\n"
        '{"summary":"what was wrong and what you changed","files":[{"path":"main.tf","content":"the complete file"}]}\n'
        "Include every listed file. content must be the complete file.\n\n"
        f"fmt log:\n{diff_text[:12000]}\n\n"
        f"current files:\n{file_bundle(paths)}"
    )
    payload = {
        "modelId": MODEL_ID,
        "system": [{"text": "You only reformat Terraform source. You never change its meaning."}],
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": 8000, "temperature": 0},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(payload, handle)
        request_path = handle.name
    try:
        result = run(
            [
                "aws",
                "bedrock-runtime",
                "converse",
                "--region",
                REGION,
                "--cli-input-json",
                f"file://{request_path}",
            ]
        )
    finally:
        Path(request_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise SystemExit(f"Bedrock call failed.\n{command_text(result)}")
    response = json.loads(result.stdout)
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    text = "\n".join(block.get("text", "") for block in blocks if isinstance(block, dict))
    try:
        return parse_model_json(text)
    except (ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"{error}\n\nBedrock reply:\n{text[:4000]}") from error


def apply_files(data: dict, allowed: list[str]) -> None:
    allowed_set = set(allowed)
    written: list[str] = []
    for item in data["files"]:
        if not isinstance(item, dict) or "path" not in item or "content" not in item:
            raise SystemExit("Bedrock file entry must contain path and content")
        path = str(item["path"]).strip().removeprefix("./")
        if path not in allowed_set:
            raise SystemExit(f"Bedrock returned a file outside the fmt failure: {path}")
        text = item["content"]
        if not isinstance(text, str):
            raise SystemExit(f"Bedrock content for {path} is not text")
        if text and not text.endswith("\n"):
            text += "\n"
        (ROOT / path).write_text(text)
        written.append(path)
    missing = allowed_set - set(written)
    if missing:
        raise SystemExit("Bedrock omitted files: " + ", ".join(sorted(missing)))


def ensure_formatted() -> bool:
    """Return True when terraform fmt had to normalize the model output."""
    code, output = terraform_fmt_diff()
    if code == 0:
        return False
    formatted = run(["terraform", "fmt", "-recursive"])
    if formatted.returncode != 0:
        raise SystemExit(f"terraform fmt failed.\n{command_text(formatted)}")
    code, output = terraform_fmt_diff()
    if code != 0:
        raise SystemExit("files are still unformatted\n" + output)
    return True


def source_sha() -> str:
    resolved = os.environ.get("CODEBUILD_RESOLVED_SOURCE_VERSION", "").strip()
    if resolved and not resolved.startswith("arn:"):
        return resolved[:7]
    result = run(["git", "rev-parse", "--short", "HEAD"])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "nosha"


def use_git_checkout(paths: list[str], token: str) -> None:
    """CodePipeline sends a zip with no .git directory. Clone main, then keep the formatted files."""
    global ROOT
    probe = run(["git", "rev-parse", "--is-inside-work-tree"])
    if probe.returncode == 0 and probe.stdout.strip() == "true":
        return
    snapshots = {path: (ROOT / path).read_text() for path in paths}
    clone = Path(tempfile.mkdtemp(prefix="fmt-heal-"))
    cloned = subprocess.run(
        [
            "git",
            "-c",
            f"http.extraheader=AUTHORIZATION: bearer {token}",
            "clone",
            "--depth",
            "1",
            "--branch",
            BASE_BRANCH,
            f"https://github.com/{REPO}.git",
            str(clone),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if cloned.returncode != 0:
        detail = redact(((cloned.stdout or "") + "\n" + (cloned.stderr or "")).strip())
        raise SystemExit(f"git clone failed.\n{detail}")
    for path, content in snapshots.items():
        target = clone / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    ROOT = clone


def commit_fix(paths: list[str], summary: str, token: str) -> str:
    use_git_checkout(paths, token)
    sha = source_sha()
    build = os.environ.get("CODEBUILD_BUILD_NUMBER", "local")
    branch = f"bedrock/fmt-{sha}-{build}"
    checkout = run(["git", "checkout", "-b", branch])
    if checkout.returncode != 0:
        raise SystemExit(f"could not create branch {branch}\n{command_text(checkout)}")
    added = run(["git", "add", "--", *paths])
    if added.returncode != 0:
        raise SystemExit(command_text(added))
    status = run(["git", "status", "--porcelain"], check=True)
    if not status.stdout.strip():
        raise SystemExit("Bedrock and terraform fmt made no file changes")
    committed = run(
        [
            "git",
            "-c",
            "user.name=Manish",
            "-c",
            "user.email=pant.manu@gmail.com",
            "commit",
            "-m",
            "Format Terraform files",
            "-m",
            summary,
        ]
    )
    if committed.returncode != 0:
        raise SystemExit(f"commit failed.\n{command_text(committed)}")
    return branch


def push_branch(branch: str, token: str) -> None:
    pushed = run(
        [
            "git",
            "-c",
            f"http.extraheader=AUTHORIZATION: bearer {token}",
            "push",
            f"https://github.com/{REPO}.git",
            f"HEAD:refs/heads/{branch}",
        ]
    )
    if pushed.returncode != 0:
        raise SystemExit(f"git push failed.\n{command_text(pushed)}")


def open_pull_request(branch: str, summary: str, diff_text: str, normalized: bool, token: str) -> str:
    note = ""
    if normalized:
        note = (
            "\n\nBedrock drafted the files. `terraform fmt` then normalized them "
            "so the pull request matches the formatter exactly.\n"
        )
    body = (
        "## Summary\n"
        f"{summary.strip()}\n"
        f"{note}\n"
        "## fmt log\n"
        "```\n"
        f"{diff_text[:8000]}\n"
        "```\n\n"
        f"Model: `{MODEL_ID}`\n"
    )
    payload = json.dumps(
        {
            "title": "Format Terraform files",
            "head": branch,
            "base": BASE_BRANCH,
            "body": body,
        }
    ).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/pulls",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "pilot-fmt-heal",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = redact(error.read().decode())
        raise SystemExit(f"GitHub pull request failed ({error.code}).\n{detail}") from error
    return data["html_url"]


def main() -> None:
    code, diff_text = terraform_fmt_diff()
    if code == 0:
        print("terraform fmt passed")
        return
    if code != 3:
        raise SystemExit(f"terraform fmt failed.\n{diff_text}")

    paths = files_from_diff(diff_text)
    if not paths:
        raise SystemExit(f"fmt failed but no Terraform files were found.\n{diff_text}")

    print("terraform fmt failed. Sending the log to Bedrock.")
    proposal = ask_bedrock(diff_text, paths)
    summary = proposal["summary"].strip()
    print("Bedrock summary:")
    print(summary)
    apply_files(proposal, paths)
    normalized = ensure_formatted()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit(
            "Format fix is ready in this build, but GITHUB_TOKEN is not set, so no pull request was opened."
        )
    branch = commit_fix(paths, summary, token)
    push_branch(branch, token)
    url = open_pull_request(branch, summary, diff_text, normalized, token)
    print(f"Pull request: {url}")
    raise SystemExit("Format check failed. The fix is in the pull request.")


if __name__ == "__main__":
    main()
