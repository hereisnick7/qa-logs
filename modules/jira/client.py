"""
Jira API client. Read-методы активны; write-методы включаются только через
JIRA_WRITE_ENABLED=true и проходят guard-валидацию.
"""

from copy import deepcopy
import html
import os
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import unicodedata
from typing import Any, Optional
from dotenv import load_dotenv
from pathlib import Path

from modules.jira.guards import assert_valid_jira_write_text

from shared.paths import workflow_home
from shared.config_loader import load_module_env

_HOME = workflow_home()
# Lookup order: config/jira/.env, config/gitlab/.env (shared host fallback), then legacy modules/.env.
load_dotenv(dotenv_path=_HOME / "config" / "jira" / ".env")
load_dotenv(dotenv_path=_HOME / "config" / "gitlab" / ".env")
load_dotenv(dotenv_path=_HOME / "modules" / "jira" / ".env")
load_dotenv(dotenv_path=_HOME / "modules" / "gitlab" / ".env")

JIRA_URL = os.getenv("JIRA_URL", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_TOKEN = os.getenv("JIRA_TOKEN", "")

JIRA_WRITE_ENABLED = os.getenv("JIRA_WRITE_ENABLED", "").lower() in {"1", "true", "yes", "on"}
CONFLUENCE_WRITE_ENABLED = os.getenv("CONFLUENCE_WRITE_ENABLED", "").lower() in {"1", "true", "yes", "on"}

# Паттерн для извлечения ключа Jira из имени ветки, например feature/DEV-123-some-title
JIRA_KEY_RE = re.compile(r"([A-Z][A-Z0-9]{1,9}-\d+)")


def extract_jira_key(text: str) -> Optional[str]:
    """Извлекает первый Jira-ключ из строки (имя ветки, заголовок MR и т.п.)."""
    m = JIRA_KEY_RE.search(text)
    return m.group(1) if m else None


def slugify_branch_summary(summary: str, max_length: int = 60) -> str:
    """
    Convert a Jira summary to a conservative kebab-case branch slug.

    Non-ASCII letters are transliterated by Unicode normalization when possible;
    the remaining unsupported characters are dropped. If nothing remains, use
    a stable fallback so the branch name still contains the Jira key.
    """
    normalized = unicodedata.normalize("NFKD", summary or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return "task"
    return slug[:max_length].strip("-") or "task"


def branch_name_for_issue(issue: dict, prefix: str = "feature") -> str:
    """Build a branch name like feature/DEV-123-kebab-summary from Jira issue data."""
    key = issue.get("key")
    fields = issue.get("fields") or {}
    summary = fields.get("summary") or key or "task"
    if not key:
        raise ValueError("Jira issue is missing key")
    clean_prefix = (prefix or "feature").strip().strip("/")
    if not clean_prefix:
        clean_prefix = "feature"
    return f"{clean_prefix}/{key}-{slugify_branch_summary(summary)}"


def adf_to_text(node: Any, depth: int = 0) -> str:
    """Рекурсивно извлекает plain-text из Atlassian Document Format (ADF)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n, depth) for n in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    text = node.get("text", "")
    content = node.get("content", [])

    if node_type == "text":
        return text
    if node_type in ("paragraph", "blockquote"):
        return adf_to_text(content, depth) + "\n"
    if node_type == "heading":
        prefix = "#" * node.get("attrs", {}).get("level", 1) + " "
        return prefix + adf_to_text(content, depth) + "\n"
    if node_type == "bulletList":
        parts = []
        for item in content:
            parts.append("  - " + adf_to_text(item.get("content", []), depth).strip())
        return "\n".join(parts) + "\n"
    if node_type == "orderedList":
        parts = []
        for i, item in enumerate(content, 1):
            parts.append(f"  {i}. " + adf_to_text(item.get("content", []), depth).strip())
        return "\n".join(parts) + "\n"
    if node_type == "listItem":
        return adf_to_text(content, depth)
    if node_type == "codeBlock":
        lang = node.get("attrs", {}).get("language", "")
        code = adf_to_text(content, depth).strip()
        return f"```{lang}\n{code}\n```\n"
    if node_type == "inlineCard":
        return node.get("attrs", {}).get("url", "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "rule":
        return "---\n"
    # doc, table, tableRow, tableCell, etc. — рекурсивно
    return adf_to_text(content, depth)


def text_to_adf(text: str) -> dict:
    """
    Convert plain text with a small Markdown subset to Atlassian Document Format.

    Supported constructs are intentionally conservative: headings, paragraphs,
    bullet/ordered lists, horizontal rules and fenced code blocks.
    """
    content: list[dict] = []
    lines = (text or "").splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            language = stripped[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            node: dict = {
                "type": "codeBlock",
                "content": [{"type": "text", "text": "\n".join(code_lines)}],
            }
            if language:
                node["attrs"] = {"language": language}
            content.append(node)
            continue

        if stripped == "---":
            content.append({"type": "rule"})
            i += 1
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            content.append({
                "type": "heading",
                "attrs": {"level": len(heading_match.group(1))},
                "content": _inline_text_nodes(heading_match.group(2)),
            })
            i += 1
            continue

        bullet_match = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet_match:
            items = []
            while i < len(lines):
                item_match = re.match(r"^[-*]\s+(.+)$", lines[i].strip())
                if not item_match:
                    break
                items.append(_list_item_node(item_match.group(1)))
                i += 1
            content.append({"type": "bulletList", "content": items})
            continue

        ordered_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if ordered_match:
            items = []
            while i < len(lines):
                item_match = re.match(r"^\d+[.)]\s+(.+)$", lines[i].strip())
                if not item_match:
                    break
                items.append(_list_item_node(item_match.group(1)))
                i += 1
            content.append({"type": "orderedList", "content": items})
            continue

        if stripped.startswith("|"):
            rows = []
            while i < len(lines):
                row_stripped = lines[i].strip()
                if not row_stripped.startswith("|"):
                    break
                rows.append(row_stripped)
                i += 1

            def _parse_row(row: str) -> list[str]:
                return [c.strip() for c in row.strip("|").split("|")]

            def _make_cell(text: str, cell_type: str) -> dict:
                return {
                    "type": cell_type,
                    "attrs": {},
                    "content": [{"type": "paragraph", "content": _inline_text_nodes(text)}],
                }

            header_row = _parse_row(rows[0]) if rows else []
            data_start = 1
            if len(rows) > 1 and re.match(r"^[\|\-\s:]+$", rows[1]):
                data_start = 2

            table_rows = [{"type": "tableRow", "content": [_make_cell(h, "tableHeader") for h in header_row]}]
            for row in rows[data_start:]:
                cells = _parse_row(row)
                table_rows.append({"type": "tableRow", "content": [_make_cell(c, "tableCell") for c in cells]})

            content.append({
                "type": "table",
                "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                "content": table_rows,
            })
            continue

        paragraph_lines = [stripped]
        i += 1
        while i < len(lines):
            next_stripped = lines[i].strip()
            if (
                not next_stripped
                or next_stripped == "---"
                or next_stripped.startswith("```")
                or next_stripped.startswith("|")
                or re.match(r"^(#{1,6})\s+(.+)$", next_stripped)
                or re.match(r"^[-*]\s+(.+)$", next_stripped)
                or re.match(r"^\d+[.)]\s+(.+)$", next_stripped)
            ):
                break
            paragraph_lines.append(next_stripped)
            i += 1
        content.append({"type": "paragraph", "content": _inline_text_nodes("\n".join(paragraph_lines))})

    if not content:
        content.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": content}


_INLINE_TOKEN_RE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\[(?P<link_text>[^\]\n]+)\]\((?P<link_href>[^)\n]+)\)"
    r"|\*\*(?P<bold>[^*\n]+)\*\*"
    r"|(?P<br>\n)"
    r"|(?P<text>[^`\[\n*]+|[*`\[])"
)


def _inline_text_nodes(text: str) -> list[dict]:
    nodes: list[dict] = []
    pending = ""

    def _flush() -> None:
        nonlocal pending
        if pending:
            nodes.append({"type": "text", "text": pending})
            pending = ""

    for m in _INLINE_TOKEN_RE.finditer(text):
        if m.group("code") is not None:
            _flush()
            nodes.append({"type": "text", "text": m.group("code"), "marks": [{"type": "code"}]})
        elif m.group("link_text") is not None:
            _flush()
            nodes.append({
                "type": "text",
                "text": m.group("link_text"),
                "marks": [{"type": "link", "attrs": {"href": m.group("link_href")}}],
            })
        elif m.group("bold") is not None:
            _flush()
            nodes.append({"type": "text", "text": m.group("bold"), "marks": [{"type": "strong"}]})
        elif m.group("br") is not None:
            _flush()
            nodes.append({"type": "hardBreak"})
        else:
            pending += m.group("text")

    _flush()
    return nodes


def _list_item_node(text: str) -> dict:
    return {
        "type": "listItem",
        "content": [{"type": "paragraph", "content": _inline_text_nodes(text)}],
    }


def _ensure_adf_doc(node: Any) -> dict:
    if isinstance(node, dict) and node.get("type") == "doc":
        doc = deepcopy(node)
        doc.setdefault("version", 1)
        doc.setdefault("content", [])
        return doc
    if not node:
        return {"type": "doc", "version": 1, "content": []}
    text = adf_to_text(node).strip()
    return text_to_adf(text)


CONFLUENCE_PAGE_RE = re.compile(r"/pages/(\d+)")


def confluence_page_id_from_ref(page_ref: str) -> str:
    """
    Extract Confluence page id from a numeric id or a page URL.

    Supported URL examples:
    - https://<domain>.atlassian.net/wiki/spaces/QA/pages/123456789
    - https://<domain>.atlassian.net/wiki/pages/123456789/Some+Title
    """
    ref = (page_ref or "").strip()
    if not ref:
        raise ValueError("Confluence page reference must not be empty")
    if ref.isdigit():
        return ref
    match = CONFLUENCE_PAGE_RE.search(ref)
    if not match:
        raise ValueError(
            "Cannot extract Confluence page id from reference. "
            "Pass numeric page id or a URL containing /pages/<id>."
        )
    return match.group(1)


def confluence_storage_to_text(storage_value: str) -> str:
    """
    Convert Confluence storage HTML to rough plain text for previews/logging.
    """
    raw = storage_value or ""
    # Remove script/style blocks conservatively.
    raw = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", raw, flags=re.I | re.S)
    # Preserve a few structural boundaries before stripping tags.
    raw = re.sub(r"</(p|div|h[1-6]|li|pre|tr|ul|ol)>", "\n", raw, flags=re.I)
    raw = raw.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def _inline_to_storage_html(text: str) -> str:
    """Render inline markdown: [label](url) → <a href="url">label</a>, rest is html-escaped."""
    result = []
    i = 0
    while i < len(text):
        if text[i] == "[":
            m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", text[i:])
            if m:
                label = html.escape(m.group(1))
                url = html.escape(m.group(2))
                result.append(f'<a href="{url}">{label}</a>')
                i += m.end()
                continue
        result.append(html.escape(text[i]))
        i += 1
    return "".join(result)


def text_to_confluence_storage(text: str) -> str:
    """
    Convert plain text with a small Markdown subset to Confluence storage HTML.
    """
    lines = (text or "").splitlines()
    blocks: list[str] = []
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            code = html.escape("\n".join(code_lines))
            blocks.append(f"<pre><code>{code}</code></pre>")
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            content = html.escape(heading_match.group(2))
            blocks.append(f"<h{level}>{content}</h{level}>")
            i += 1
            continue

        if stripped == "---":
            blocks.append("<hr />")
            i += 1
            continue

        if re.match(r"^[-*]\s+(.+)$", stripped):
            items = []
            while i < len(lines):
                item_match = re.match(r"^[-*]\s+(.+)$", lines[i].strip())
                if not item_match:
                    break
                items.append(f"<li>{html.escape(item_match.group(1))}</li>")
                i += 1
            blocks.append("<ul>" + "".join(items) + "</ul>")
            continue

        if re.match(r"^\d+[.)]\s+(.+)$", stripped):
            items = []
            while i < len(lines):
                item_match = re.match(r"^\d+[.)]\s+(.+)$", lines[i].strip())
                if not item_match:
                    break
                items.append(f"<li>{html.escape(item_match.group(1))}</li>")
                i += 1
            blocks.append("<ol>" + "".join(items) + "</ol>")
            continue

        if stripped.startswith("|"):
            rows = []
            is_header_row = True
            while i < len(lines):
                row_stripped = lines[i].strip()
                if not row_stripped.startswith("|"):
                    break
                # Skip separator rows like |---|---|
                if re.match(r"^\|[\s\-:|]+\|", row_stripped):
                    i += 1
                    is_header_row = False
                    continue
                cells = [c.strip() for c in row_stripped.strip("|").split("|")]
                tag = "th" if is_header_row else "td"
                cell_html = "".join(f"<{tag}>{_inline_to_storage_html(c)}</{tag}>" for c in cells)
                rows.append(f"<tr>{cell_html}</tr>")
                i += 1
                is_header_row = False
            blocks.append("<table><tbody>" + "".join(rows) + "</tbody></table>")
            continue

        paragraph_lines = [html.escape(stripped)]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith("```")
                or nxt == "---"
                or nxt.startswith("|")
                or re.match(r"^(#{1,6})\s+(.+)$", nxt)
                or re.match(r"^[-*]\s+(.+)$", nxt)
                or re.match(r"^\d+[.)]\s+(.+)$", nxt)
            ):
                break
            paragraph_lines.append(html.escape(nxt))
            i += 1
        blocks.append("<p>" + "<br/>".join(paragraph_lines) + "</p>")

    return "".join(blocks) or "<p></p>"


class JiraClient:
    def __init__(self, account: str = "default"):
        env = load_module_env("jira", account)
        url = env.get("JIRA_URL", "")
        email = env.get("JIRA_EMAIL", "")
        token = env.get("JIRA_TOKEN", "")
        if not url or not email or not token:
            raise RuntimeError(
                f"Set JIRA_URL, JIRA_EMAIL, JIRA_TOKEN for account '{account}' in "
                f"config/jira/.env (bare keys, or JIRA__{account.upper()}__* for multi-account)"
            )
        self.account = account
        self.base_url = url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        _adapter = HTTPAdapter(
            max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504]),
        )
        self.session.mount("https://", _adapter)
        self.session.mount("http://", _adapter)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/rest/api/3{path}"
        resp = self.session.get(url, params=params, timeout=(5, 15))
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/rest/api/3{path}"
        resp = self.session.post(url, json=data, timeout=(5, 15))
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}

    def _put(self, path: str, data: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/rest/api/3{path}"
        resp = self.session.put(url, json=data, timeout=(5, 15))
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}

    def _write_guard(self):
        if not JIRA_WRITE_ENABLED:
            raise NotImplementedError(
                "Jira write operations are disabled. Set JIRA_WRITE_ENABLED=true in .env "
                "and request the write action explicitly in the current chat."
            )

    def _confluence_write_guard(self):
        if not CONFLUENCE_WRITE_ENABLED:
            raise NotImplementedError(
                "Confluence write operations are disabled. Set CONFLUENCE_WRITE_ENABLED=true "
                "in jira/.env and request the write action explicitly in the current chat."
            )

    def _confluence_get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/wiki/rest/api{path}"
        resp = self.session.get(url, params=params, timeout=(5, 20))
        resp.raise_for_status()
        return resp.json()

    def _confluence_post(self, path: str, data: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/wiki/rest/api{path}"
        resp = self.session.post(url, json=data, timeout=(5, 20))
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}

    def _confluence_put(self, path: str, data: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/wiki/rest/api{path}"
        resp = self.session.put(url, json=data, timeout=(5, 20))
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}

    # ------------------------------------------------------------------
    # User / auth
    # ------------------------------------------------------------------

    def current_user(self) -> dict:
        return self._get("/myself")

    # ------------------------------------------------------------------
    # Issues — read
    # ------------------------------------------------------------------

    def get_issue(self, key: str) -> dict:
        return self._get(f"/issue/{key}")

    def list_attachments(self, key: str) -> list:
        """Return issue attachments as list of dicts with id/filename/mimeType/size/created/author/content."""
        issue = self._get(f"/issue/{key}", {"fields": "attachment"})
        return ((issue.get("fields") or {}).get("attachment")) or []

    def download_attachment(self, content_url: str, dest_path: str) -> str:
        """
        Stream attachment binary from Jira to dest_path. content_url is the
        authenticated URL returned by Jira (attachment["content"]).
        Returns the absolute path written.
        """
        from pathlib import Path as _Path
        dest = _Path(dest_path).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.session.get(content_url, stream=True, timeout=(5, 60), allow_redirects=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        return str(dest)

    def search(self, jql: str, fields: Optional[list] = None, max_results: int = 50) -> dict:
        # POST /rest/api/3/search/jql — актуальный эндпоинт (GET /search — 410 Gone)
        # Без явного fields API возвращает только id; передаём дефолтный набор.
        default_fields = [
            "summary", "status", "priority", "issuetype", "assignee", "reporter",
            "labels", "components", "created", "updated", "description", "comment",
        ]
        payload: dict = {"jql": jql, "maxResults": max_results, "fields": fields or default_fields}
        return self._post("/search/jql", payload)

    def my_issues(self, status: Optional[str] = None) -> list:
        """Мои активные задачи. status=None → все кроме Done."""
        if status:
            jql = f'assignee = currentUser() AND status = "{status}" ORDER BY updated DESC'
        else:
            jql = "assignee = currentUser() AND status != Done ORDER BY updated DESC"
        return self.search(jql).get("issues", [])

    def issues_by_status(self, project: str, status: str) -> list:
        jql = f'project = {project} AND status = "{status}" ORDER BY updated DESC'
        return self.search(jql).get("issues", [])

    def get_transitions(self, key: str) -> list:
        """Доступные переходы для задачи (полезно для будущего write)."""
        return self._get(f"/issue/{key}/transitions").get("transitions", [])

    def get_comments(self, key: str, max_results: int = 50) -> list:
        """
        Read comments on an issue, oldest first. Each item: id/author/created/updated/body (plain text).
        Read-only — no write guard. Used to read back a QA comment after the user has
        edited it (checkboxes ticked, items struck through/removed) as ground truth
        for what was actually tested.
        """
        result = self._get(f"/issue/{key}/comment", {"maxResults": max_results, "orderBy": "created"})
        out = []
        for c in result.get("comments", []):
            out.append({
                "id": c.get("id"),
                "author": (c.get("author") or {}).get("displayName"),
                "created": c.get("created"),
                "updated": c.get("updated"),
                "body": adf_to_text(c.get("body")).strip(),
            })
        return out

    def _dev_status_get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}/rest/dev-status/1.0{path}"
        resp = self.session.get(url, params=params, timeout=(5, 15))
        resp.raise_for_status()
        return resp.json()

    def get_dev_status(self, key: str) -> list:
        """
        Linked merge requests from the Development panel (Jira↔GitLab integration).
        Returns a list of dicts: id ("path/to/repo!N"), status, url, repository_name,
        source_branch, target_branch, last_update. Empty list = no linked MR, not an error.

        Confirmed live 2026-07-03 against DEV-2842. Two things the standard Jira Cloud
        dev-status docs don't make obvious:
        - The `applicationType` id is NOT the literal string "GitLab" — it's whatever
          this instance's connect app is registered as (here:
          "oAuth-gitlab-jira-connect-gitlab.com"). Auto-discovered via /issue/summary
          instead of hardcoded, since it can differ per Jira instance/integration setup.
        - Pull requests live at `detail[].pullRequests[]`, not nested under
          `detail[].repositories[].pullRequests[]` as originally assumed.
        """
        issue = self._get(f"/issue/{key}", {"fields": "summary"})
        issue_id = issue.get("id")
        summary = self._dev_status_get("/issue/summary", {"issueId": issue_id})
        instance_types = list(
            (summary.get("summary", {}).get("pullrequest", {}) or {}).get("byInstanceType", {}).keys()
        )
        if not instance_types:
            return []

        out = []
        for app_type in instance_types:
            detail = self._dev_status_get(
                "/issue/detail",
                {"issueId": issue_id, "applicationType": app_type, "dataType": "pullrequest"},
            )
            for d in detail.get("detail", []):
                for pr in d.get("pullRequests", []):
                    out.append({
                        "id": pr.get("id"),
                        "status": pr.get("status"),
                        "url": pr.get("url"),
                        "repository_name": pr.get("repositoryName"),
                        "source_branch": (pr.get("source") or {}).get("branch"),
                        "target_branch": (pr.get("destination") or {}).get("branch"),
                        "last_update": pr.get("lastUpdate"),
                    })
        return out

    # ------------------------------------------------------------------
    # Write — guarded and opt-in
    # ------------------------------------------------------------------

    def create_issue(
        self,
        project_key: str,
        summary: str,
        *,
        description: str = "",
        issue_type: str = "Task",
        priority: Optional[str] = None,
        labels: Optional[list] = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Create a Jira issue. Requires JIRA_WRITE_ENABLED=true.

        Returns {"key": "DEV-1234", "id": "...", "url": "..."} on success.
        """
        self._write_guard()
        clean_summary = (summary or "").strip()
        if not clean_summary:
            raise ValueError("Issue summary must not be empty")
        assert_valid_jira_write_text(clean_summary)
        if description:
            assert_valid_jira_write_text(description)

        fields: dict = {
            "project": {"key": project_key.upper()},
            "summary": clean_summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = text_to_adf(description)
        if priority:
            fields["priority"] = {"name": priority}
        if labels:
            fields["labels"] = list(labels)

        payload = {"fields": fields}
        if dry_run:
            return {
                "dry_run": True,
                "project_key": project_key.upper(),
                "summary": clean_summary,
                "issue_type": issue_type,
                "priority": priority,
                "labels": labels or [],
                "description": description,
                "payload": payload,
            }
        result = self._post("/issue", payload)
        key = result.get("key", "")
        return {
            "key": key,
            "id": result.get("id"),
            "url": f"{self.base_url}/browse/{key}" if key else None,
        }

    def update_issue(self, *args, **kwargs):
        self._write_guard()
        raise NotImplementedError("Full Jira issue update is not exposed; use append_description")

    def append_description(
        self,
        key: str,
        text: str,
        *,
        separator: str = "\n\n---\n",
        dry_run: bool = False,
    ) -> dict:
        self._write_guard()
        assert_valid_jira_write_text(text)
        issue = self._get(f"/issue/{key}", {"fields": "description"})
        current = ((issue.get("fields") or {}).get("description"))
        description = _ensure_adf_doc(current)

        if separator == "\n\n---\n":
            addition_nodes = [{"type": "rule"}, *text_to_adf(text)["content"]]
        else:
            addition_nodes = text_to_adf(f"{separator}{text}")["content"]
        new_description = {
            **description,
            "content": [*description.get("content", []), *addition_nodes],
        }
        payload = {"fields": {"description": new_description}}
        if dry_run:
            return {
                "dry_run": True,
                "key": key,
                "current_text": adf_to_text(current).strip(),
                "append_text": text,
                "payload": payload,
            }
        result = self._put(f"/issue/{key}", payload)
        return {"key": key, "updated": True, "result": result}

    def set_description(self, key: str, text: str, *, dry_run: bool = False) -> dict:
        """
        Replace issue description with new ADF content.
        """
        self._write_guard()
        assert_valid_jira_write_text(text)
        issue = self._get(f"/issue/{key}", {"fields": "description"})
        current = ((issue.get("fields") or {}).get("description"))
        payload = {"fields": {"description": text_to_adf(text)}}
        if dry_run:
            return {
                "dry_run": True,
                "key": key,
                "current_text": adf_to_text(current).strip(),
                "new_text": text,
                "payload": payload,
            }
        result = self._put(f"/issue/{key}", payload)
        return {"key": key, "updated": True, "result": result}

    def add_comment(self, key: str, text: str, *, dry_run: bool = False) -> dict:
        self._write_guard()
        assert_valid_jira_write_text(text)
        payload = {"body": text_to_adf(text)}
        if dry_run:
            return {"dry_run": True, "key": key, "text": text, "payload": payload}
        comment = self._post(f"/issue/{key}/comment", payload)
        return {"key": key, "comment": comment}

    def transition_issue(
        self,
        key: str,
        target: str,
        *,
        comment: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        self._write_guard()
        target_clean = (target or "").strip()
        if not target_clean:
            raise ValueError("Target transition/status must not be empty")
        if comment is not None:
            assert_valid_jira_write_text(comment)

        transitions = self.get_transitions(key)
        match = next(
            (t for t in transitions if (t.get("name") or "").lower() == target_clean.lower()),
            None,
        )
        if not match:
            available = ", ".join(t.get("name", "?") for t in transitions) or "none"
            raise ValueError(f"Transition '{target}' is not available for {key}. Available: {available}")

        payload: dict = {"transition": {"id": match["id"]}}
        if comment:
            payload["update"] = {"comment": [{"add": {"body": text_to_adf(comment)}}]}
        if dry_run:
            return {
                "dry_run": True,
                "key": key,
                "target": match.get("name"),
                "transition_id": match.get("id"),
                "payload": payload,
            }
        result = self._post(f"/issue/{key}/transitions", payload)
        return {"key": key, "target": match.get("name"), "transition_id": match.get("id"), "result": result}

    # ------------------------------------------------------------------
    # Confluence docs — read + guarded write
    # ------------------------------------------------------------------

    def get_confluence_page(self, page_ref: str) -> dict:
        page_id = confluence_page_id_from_ref(page_ref)
        return self._confluence_get(f"/content/{page_id}", {"expand": "version,space,body.storage"})

    def update_confluence_page(
        self,
        page_ref: str,
        text: str,
        *,
        title: Optional[str] = None,
        mode: str = "append",
        dry_run: bool = False,
    ) -> dict:
        """
        Update existing Confluence page body.

        mode:
        - append: keep current storage and append new section separated by <hr />
        - replace: replace body with only new content
        """
        self._confluence_write_guard()
        assert_valid_jira_write_text(text)

        mode_clean = (mode or "append").strip().lower()
        if mode_clean not in {"append", "replace"}:
            raise ValueError("Confluence update mode must be 'append' or 'replace'")

        page = self.get_confluence_page(page_ref)
        page_id = page.get("id")
        page_title = (title or page.get("title") or "").strip()
        if not page_title:
            raise ValueError("Confluence page title must not be empty")

        current_storage = (((page.get("body") or {}).get("storage") or {}).get("value") or "")
        new_section = text_to_confluence_storage(text)
        if mode_clean == "append":
            merged_storage = f"{current_storage}\n<hr />\n{new_section}" if current_storage else new_section
        else:
            merged_storage = new_section

        current_version = int(((page.get("version") or {}).get("number")) or 1)
        space_key = ((page.get("space") or {}).get("key")) or ""
        payload: dict = {
            "id": page_id,
            "type": "page",
            "title": page_title,
            "space": {"key": space_key},
            "body": {"storage": {"value": merged_storage, "representation": "storage"}},
            "version": {"number": current_version + 1},
        }

        if dry_run:
            return {
                "dry_run": True,
                "page_id": page_id,
                "title": page_title,
                "mode": mode_clean,
                "current_text": confluence_storage_to_text(current_storage),
                "append_text": text if mode_clean == "append" else None,
                "new_text": text if mode_clean == "replace" else None,
                "payload": payload,
            }

        result = self._confluence_put(f"/content/{page_id}", payload)
        return {
            "page_id": page_id,
            "title": result.get("title", page_title),
            "version": ((result.get("version") or {}).get("number")),
            "url": (
                f"{self.base_url}/wiki/spaces/{space_key}/pages/{page_id}"
                if space_key else f"{self.base_url}/wiki/pages/{page_id}"
            ),
        }

    def create_confluence_page(
        self,
        space_key: str,
        title: str,
        text: str,
        *,
        parent_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict:
        self._confluence_write_guard()
        assert_valid_jira_write_text(text)

        clean_space = (space_key or "").strip()
        clean_title = (title or "").strip()
        if not clean_space:
            raise ValueError("Confluence space key must not be empty")
        if not clean_title:
            raise ValueError("Confluence page title must not be empty")

        payload: dict = {
            "type": "page",
            "title": clean_title,
            "space": {"key": clean_space},
            "body": {"storage": {"value": text_to_confluence_storage(text), "representation": "storage"}},
        }
        if parent_id:
            payload["ancestors"] = [{"id": str(parent_id)}]

        if dry_run:
            return {
                "dry_run": True,
                "space_key": clean_space,
                "title": clean_title,
                "parent_id": str(parent_id) if parent_id else None,
                "text": text,
                "payload": payload,
            }

        result = self._confluence_post("/content", payload)
        page_id = result.get("id")
        return {
            "page_id": page_id,
            "title": result.get("title", clean_title),
            "space_key": clean_space,
            "url": (
                f"{self.base_url}/wiki/spaces/{clean_space}/pages/{page_id}"
                if page_id else None
            ),
        }
