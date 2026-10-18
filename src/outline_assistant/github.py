"""Bounded GitHub REST requests. Credentials stay in the process environment."""
import os
import re
import time

import httpx

from .config import AppError


class GitHubError(AppError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, settings, transport=None):
        self.settings = settings
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": settings.github_api_version}
        if token:
            headers["Authorization"] = "Bearer " + token
        self.authenticated = bool(token)
        self.client = httpx.Client(base_url=settings.github_api_url.rstrip("/"), headers=headers, timeout=settings.github_timeout, trust_env=False, transport=transport)

    def close(self):
        self.client.close()

    def check_repo(self, repo):
        if repo != self.settings.github_repo:
            raise AppError("Репозиторий не совпадает с разрешённым github_repo.")

    def request(self, method, path, *, body=None, params=None):
        if not path.startswith("/") or path.startswith("//"):
            raise AppError("Допустим только относительный GitHub API endpoint.")
        # Never retry mutations: an ambiguous POST timeout must not create duplicates.
        attempts = self.settings.github_retries + 1 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.client.request(method, path, json=body, params=params)
            except httpx.HTTPError as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(attempt + 1, 2))
                    continue
                raise GitHubError("GitHub недоступен или истёк таймаут; проверьте сеть и github_api_url.") from exc
            if response.status_code >= 500 or response.status_code == 429:
                if attempt + 1 < attempts:
                    time.sleep(min(attempt + 1, 2))
                    continue
            if response.is_error:
                descriptions = {401: "Нет действительной аутентификации GitHub.", 403: "GitHub отказал в доступе (права токена или rate limit).", 404: "Репозиторий/PR не найден либо недоступен этому токену.", 422: "GitHub отклонил параметры PR или публикации."}
                raise GitHubError(descriptions.get(response.status_code, f"GitHub HTTP {response.status_code}."), response.status_code)
            try:
                return response.json(), response
            except ValueError as exc:
                raise GitHubError("GitHub вернул неверный JSON.") from exc

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)[0]

    def pages(self, path, *, strict=True):
        items = []
        for page in range(1, self.settings.github_max_pages + 1):
            data, response = self.request("GET", path, params={"per_page": 100, "page": page})
            if not isinstance(data, list):
                raise GitHubError("Ожидался список GitHub API.")
            items.extend(data)
            more = 'rel="next"' in response.headers.get("link", "")
            if not more and len(data) < 100:
                return items, False
            if not more and "link" in response.headers:
                return items, False
        if strict:
            raise GitHubError("Достигнут лимит страниц GitHub; увеличьте github_max_pages.")
        return items, True

    def pr(self, repo, number):
        self.check_repo(repo)
        if type(number) is not int or number <= 0:
            raise AppError("Номер PR должен быть положительным целым числом.")
        data = self.get(f"/repos/{repo}/pulls/{number}")
        try:
            result = {"number": data["number"], "state": data["state"], "title": data["title"], "body": data.get("body") or "", "draft": data.get("draft", False), "author": data["user"]["login"], "changed_files": data["changed_files"], "repo": repo}
            for side in ("base", "head"):
                result[side + "_repo"] = data[side]["repo"]["full_name"]
                result[side + "_sha"] = data[side]["sha"]
                if not re.fullmatch(r"[0-9a-f]{40}", result[side + "_sha"]):
                    raise ValueError("SHA")
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("Некорректные метаданные PR или удалён head-репозиторий.") from exc

    def files(self, repo, number):
        self.check_repo(repo)
        rows, truncated = self.pages(f"/repos/{repo}/pulls/{number}/files", strict=False)
        return {"files": [{"path": r["filename"], "previous_path": r.get("previous_filename"), "status": r["status"], "additions": r["additions"], "deletions": r["deletions"], "patch_available": bool(r.get("patch"))} for r in rows], "incomplete": truncated or len(rows) >= 3000, "patches_authoritative": False}


def trusted_pr(info, repo, owner="metalice80"):
    return info["state"] == "open" and not info["draft"] and info["author"] == owner and info["base_repo"] == repo and info["head_repo"] == repo
