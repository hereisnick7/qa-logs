"""
Jira + Confluence CLI commands. Called from cli/agent.py dispatcher.
run_command() returns True if the command was handled, False otherwise.
"""

from __future__ import annotations

import sys
import argparse
from pathlib import Path

_WORKFLOW_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_WORKFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKFLOW_ROOT))

_jira_clients: dict = {}
_active_account = "default"


def _get_jira(account: str | None = None):
    """Return a JiraClient for `account` (or the active one), cached per account."""
    acc = account or _active_account
    if acc not in _jira_clients:
        from modules.jira.client import JiraClient
        _jira_clients[acc] = JiraClient(account=acc)
    return _jira_clients[acc]


def _resolve_active_account(parts: list) -> str:
    """
    Pick the Jira account for this command:
      1. explicit --account=<name>;
      2. account of the project owning the first Jira key in the args;
      3. WORKFLOW_ACCOUNT env / "default" (via resolve_account).
    """
    from shared.config_loader import resolve_account
    explicit = next((p.split("=", 1)[1] for p in parts if p.startswith("--account=")), None)
    if explicit:
        return explicit
    from modules.jira.client import extract_jira_key
    for token in parts[1:]:
        key = extract_jira_key(token.upper())
        if key:
            return resolve_account("jira", jira_key=key)
    return resolve_account("jira")


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------

def fmt_issue_short(issue: dict) -> str:
    fields = issue.get("fields", {})
    key = issue.get("key", "?")
    summary = fields.get("summary", "")
    status = (fields.get("status") or {}).get("name", "?")
    priority = (fields.get("priority") or {}).get("name", "?")
    assignee = (fields.get("assignee") or {}).get("displayName", "—")
    return f"  {key:<14} [{status:<14}]  {summary}\n               Приоритет: {priority}  |  Assignee: {assignee}\n"


def fmt_issue_detail(issue: dict) -> str:
    from modules.jira.client import adf_to_text
    fields = issue.get("fields", {})
    key = issue.get("key", "?")
    summary = fields.get("summary", "")
    status = (fields.get("status") or {}).get("name", "?")
    priority = (fields.get("priority") or {}).get("name", "?")
    issue_type = (fields.get("issuetype") or {}).get("name", "?")
    assignee = (fields.get("assignee") or {}).get("displayName", "—")
    reporter = (fields.get("reporter") or {}).get("displayName", "—")
    labels = ", ".join(fields.get("labels") or []) or "—"
    components = ", ".join(c.get("name", "") for c in (fields.get("components") or [])) or "—"
    created = (fields.get("created") or "")[:10]
    updated = (fields.get("updated") or "")[:10]

    lines = [
        f"\n{'='*60}",
        f"  {key}  [{issue_type}]  {summary}",
        f"{'='*60}",
        f"  Статус:     {status}",
        f"  Приоритет:  {priority}",
        f"  Assignee:   {assignee}",
        f"  Reporter:   {reporter}",
        f"  Метки:      {labels}",
        f"  Компоненты: {components}",
        f"  Создана:    {created}  |  Обновлена: {updated}",
    ]

    desc_raw = fields.get("description")
    if desc_raw:
        desc_text = adf_to_text(desc_raw).strip()
        if desc_text:
            lines.append("\n  Описание:")
            for line in desc_text.splitlines():
                lines.append(f"    {line}")

    comments_data = (fields.get("comment") or {})
    comments = comments_data.get("comments", []) if isinstance(comments_data, dict) else []
    if comments:
        lines.append(f"\n  Комментарии (последние {min(3, len(comments))} из {len(comments)}):")
        for c in comments[-3:]:
            author = (c.get("author") or {}).get("displayName", "?")
            date = (c.get("updated") or c.get("created") or "")[:10]
            body = adf_to_text(c.get("body")).strip()
            lines.append(f"\n    [{date}] {author}:")
            for line in body.splitlines()[:10]:
                lines.append(f"      {line}")

    attachments = fields.get("attachment") or []
    if attachments:
        lines.append(f"\n  Вложения ({len(attachments)}):")
        for a in attachments:
            size = a.get("size") or 0
            size_kb = f"{size / 1024:.1f}KB" if size else "?"
            mime = a.get("mimeType", "?")
            author = (a.get("author") or {}).get("displayName", "?")
            date = (a.get("created") or "")[:10]
            lines.append(f"    [{a.get('id')}] {a.get('filename')}  ({mime}, {size_kb}, {date}, {author})")

    lines.append(f"\n  URL: {issue.get('self', '').split('/rest/')[0]}/browse/{key}")
    lines.append(f"{'='*60}\n")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _read_text_arg(text: str = None, from_file: str = None) -> str:
    if from_file:
        with open(from_file, "r", encoding="utf-8") as f:
            return f.read()
    return text or ""


# ------------------------------------------------------------------
# Command implementations
# ------------------------------------------------------------------

def cmd_jira_info():
    u = _get_jira().current_user()
    print(f"\n{'='*50}")
    print(f"  Пользователь Jira: {u.get('displayName')} (@{u.get('emailAddress')})")
    print(f"  Account ID: {u.get('accountId')}")
    print(f"{'='*50}\n")


def cmd_jira_mine(status: str = None):
    issues = _get_jira().my_issues(status=status)
    label = f"со статусом «{status}»" if status else "(кроме Done)"
    print(f"\n  Мои задачи {label} ({len(issues)}):\n")
    if not issues:
        print("  (нет)\n")
        return
    for issue in issues:
        print(fmt_issue_short(issue))


def cmd_jira_task(key: str):
    print(fmt_issue_detail(_get_jira().get_issue(key)))


def cmd_jira_search(jql: str):
    result = _get_jira().search(jql)
    issues = result.get("issues", [])
    total = result.get("total", len(issues))
    print(f"\n  Результаты JQL «{jql}» ({len(issues)} из {total}):\n")
    if not issues:
        print("  (нет)\n")
        return
    for issue in issues:
        print(fmt_issue_short(issue))


def cmd_jira_transitions(key: str):
    transitions = _get_jira().get_transitions(key)
    print(f"\n  Доступные transitions для {key} ({len(transitions)}):\n")
    if not transitions:
        print("  (нет)\n")
        return
    for transition in transitions:
        to_status = (transition.get("to") or {}).get("name")
        suffix = f" -> {to_status}" if to_status else ""
        print(f"  {transition.get('id'):<6} {transition.get('name', '?')}{suffix}")
    print()


def cmd_jira_append(key: str, text: str, from_file: str = None, dry_run: bool = False):
    append_text = _read_text_arg(text, from_file)
    result = _get_jira().append_description(key, append_text, dry_run=dry_run)
    if dry_run:
        current_tail = result.get("current_text", "")[-800:] or "—"
        print(f"\n  DRY RUN: описание {key} будет дополнено.\n")
        print("  Текущий хвост описания:")
        for line in current_tail.splitlines()[-20:]:
            print(f"    {line}")
        print("\n  Будет добавлено:")
        for line in append_text.splitlines():
            print(f"    {line}")
        print()
        return
    print(f"\n  Описание {key} дополнено.\n")


def cmd_jira_set_description(key: str, text: str, from_file: str = None, dry_run: bool = False):
    description_text = _read_text_arg(text, from_file)
    result = _get_jira().set_description(key, description_text, dry_run=dry_run)
    if dry_run:
        current_tail = result.get("current_text", "")[-800:] or "—"
        print(f"\n  DRY RUN: описание {key} будет полностью заменено.\n")
        print("  Текущий хвост описания:")
        for line in current_tail.splitlines()[-20:]:
            print(f"    {line}")
        print("\n  Новое описание:")
        for line in description_text.splitlines():
            print(f"    {line}")
        print()
        return
    print(f"\n  Описание {key} обновлено.\n")


def cmd_jira_comment(key: str, text: str, from_file: str = None, dry_run: bool = False):
    comment_text = _read_text_arg(text, from_file)
    result = _get_jira().add_comment(key, comment_text, dry_run=dry_run)
    if dry_run:
        print(f"\n  DRY RUN: комментарий в {key}.\n")
        for line in comment_text.splitlines():
            print(f"    {line}")
        print()
        return
    comment = result.get("comment") or {}
    print(f"\n  Комментарий добавлен в {key} (id: {comment.get('id', '—')}).\n")


def cmd_jira_transition(key: str, target: str, comment: str = None, comment_from_file: str = None, dry_run: bool = False):
    comment_text = _read_text_arg(comment, comment_from_file) if (comment or comment_from_file) else None
    result = _get_jira().transition_issue(key, target, comment=comment_text, dry_run=dry_run)
    if dry_run:
        print(f"\n  DRY RUN: {key} будет переведён в «{result.get('target')}».")
        if comment_text:
            print("\n  Комментарий при transition:")
            for line in comment_text.splitlines():
                print(f"    {line}")
        print()
        return
    print(f"\n  {key} переведён в «{result.get('target')}».\n")


def cmd_jira_comments(key: str):
    comments = _get_jira().get_comments(key)
    print(f"\n  Комментарии {key} ({len(comments)}):\n")
    if not comments:
        print("  (нет)\n")
        return
    for c in comments:
        date = (c.get("created") or "")[:16].replace("T", " ")
        print(f"  [{c.get('id')}] {c.get('author', '?')} · {date}")
        for line in (c.get("body") or "").splitlines():
            print(f"      {line}")
        print()


def cmd_jira_dev_status(key: str):
    try:
        prs = _get_jira().get_dev_status(key)
    except Exception as e:
        print(f"\n  dev-status недоступен для {key}: {e}\n")
        return
    if not prs:
        print(f"\n  Для {key} нет привязанного MR в Development panel.\n")
        return
    print(f"\n  Development panel — {key}:\n")
    for pr in prs:
        print(f"  {pr['id']} · {pr['status']} · {pr['repository_name']}")
        print(f"      {pr['source_branch']} → {pr['target_branch']}")
        print(f"      {pr['url']}")
    print()


def cmd_jira_attachments(key: str):
    attachments = _get_jira().list_attachments(key)
    print(f"\n  Вложения {key} ({len(attachments)}):\n")
    if not attachments:
        print("  (нет)\n")
        return
    for a in attachments:
        size = a.get("size") or 0
        size_kb = f"{size / 1024:.1f}KB" if size else "?"
        mime = a.get("mimeType", "?")
        author = (a.get("author") or {}).get("displayName", "?")
        date = (a.get("created") or "")[:10]
        print(f"  [{a.get('id')}] {a.get('filename')}")
        print(f"      {mime} | {size_kb} | {date} | {author}")
        print(f"      content: {a.get('content')}")
    print()


def cmd_jira_download(key: str, attachment_id: str = None, name: str = None, out: str = None, all_files: bool = False):
    import os
    from pathlib import Path as _Path

    jira = _get_jira()
    attachments = jira.list_attachments(key)
    if not attachments:
        print(f"\n  У {key} нет вложений.\n")
        return

    if all_files:
        targets = attachments
    elif attachment_id:
        targets = [a for a in attachments if str(a.get("id")) == str(attachment_id)]
    elif name:
        targets = [a for a in attachments if (a.get("filename") or "").lower() == name.lower()]
        if not targets:
            targets = [a for a in attachments if name.lower() in (a.get("filename") or "").lower()]
    else:
        if len(attachments) == 1:
            targets = attachments
        else:
            print(f"\n  У {key} {len(attachments)} вложений. Уточни --id=<id>, --name=<filename> или --all.\n")
            for a in attachments:
                print(f"  [{a.get('id')}] {a.get('filename')}")
            print()
            return

    if not targets:
        print(f"\n  Подходящих вложений не найдено в {key}.\n")
        return

    if out:
        out_dir = _Path(out).expanduser().resolve()
    else:
        from shared.paths import jira_attachments_dir
        out_dir = jira_attachments_dir(key)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  Скачиваем в {out_dir}:")
    for a in targets:
        filename = a.get("filename") or f"attachment-{a.get('id')}"
        dest = out_dir / filename
        try:
            path = jira.download_attachment(a.get("content"), str(dest))
            size = os.path.getsize(path)
            print(f"    ok  [{a.get('id')}] {filename}  ({size / 1024:.1f}KB)  -> {path}")
        except Exception as e:
            print(f"    err [{a.get('id')}] {filename}: {e}")
    print()


def cmd_confluence_page(page: str):
    from modules.jira.client import confluence_storage_to_text
    jira = _get_jira()
    data = jira.get_confluence_page(page)
    page_id = data.get("id")
    title = data.get("title", "")
    space_key = ((data.get("space") or {}).get("key")) or "—"
    version = ((data.get("version") or {}).get("number")) or "—"
    storage = (((data.get("body") or {}).get("storage") or {}).get("value")) or ""
    preview = confluence_storage_to_text(storage)
    print(f"\n{'='*60}")
    print(f"  Confluence page {page_id}: {title}")
    print(f"{'='*60}")
    print(f"  Space:   {space_key}")
    print(f"  Version: {version}")
    print(f"  URL:     {jira.base_url}/wiki/spaces/{space_key}/pages/{page_id}")
    if preview:
        print("\n  Содержимое (preview):")
        for line in preview.splitlines()[:40]:
            print(f"    {line}")
        if len(preview.splitlines()) > 40:
            print("    ...")
    print(f"{'='*60}\n")


def cmd_confluence_update(page: str, text: str, from_file: str = None, title: str = None, mode: str = "append", dry_run: bool = False):
    update_text = _read_text_arg(text, from_file)
    result = _get_jira().update_confluence_page(page, update_text, title=title, mode=mode, dry_run=dry_run)
    if dry_run:
        print(f"\n  DRY RUN: страница {result.get('page_id')} будет обновлена (mode={result.get('mode')}).")
        print(f"  Title: {result.get('title')}")
        if result.get("mode") == "append":
            print("\n  Будет добавлено:")
            for line in update_text.splitlines():
                print(f"    {line}")
        else:
            print("\n  Новое содержимое:")
            for line in update_text.splitlines():
                print(f"    {line}")
        print()
        return
    print("\n  Confluence-страница обновлена:")
    print(f"  id:    {result.get('page_id')}")
    print(f"  title: {result.get('title')}")
    print(f"  url:   {result.get('url')}\n")


def cmd_confluence_create(space: str, title: str, text: str, from_file: str = None, parent_id: str = None, dry_run: bool = False):
    body_text = _read_text_arg(text, from_file)
    result = _get_jira().create_confluence_page(space, title, body_text, parent_id=parent_id, dry_run=dry_run)
    if dry_run:
        print(f"\n  DRY RUN: будет создана Confluence-страница '{result.get('title')}' в space {result.get('space_key')}.")
        if result.get("parent_id"):
            print(f"  parent_id: {result.get('parent_id')}")
        print()
        return
    print("\n  Confluence-страница создана:")
    print(f"  id:    {result.get('page_id')}")
    print(f"  title: {result.get('title')}")
    print(f"  url:   {result.get('url')}\n")


# ------------------------------------------------------------------
# Command router
# ------------------------------------------------------------------

JIRA_COMMANDS = {
    "jira-info", "jira-mine", "jira-task", "jira-search", "jira-transitions",
    "jira-attachments", "jira-download", "jira-comments", "jira-dev-status",
    "jira-append", "jira-set-description", "jira-comment", "jira-transition",
    "confluence-page", "confluence-update", "confluence-create",
}


def run_command(cmd: str, parts: list) -> bool:
    """Route a Jira/Confluence command. Returns True if handled, False otherwise."""
    if cmd not in JIRA_COMMANDS:
        return False

    global _active_account
    _active_account = _resolve_active_account(parts)

    try:
        if cmd == "jira-info":
            cmd_jira_info()
        elif cmd == "jira-mine":
            status_flag = next((p[9:] for p in parts[1:] if p.startswith("--status=")), None)
            cmd_jira_mine(status=status_flag)
        elif cmd == "jira-task":
            if len(parts) < 2:
                print("  Использование: jira-task <KEY>  (например: jira-task DEV-123)")
            else:
                cmd_jira_task(parts[1].upper())
        elif cmd == "jira-search":
            if len(parts) < 2:
                print('  Использование: jira-search "<JQL>"')
            else:
                cmd_jira_search(" ".join(parts[1:]).strip('"\''))
        elif cmd == "jira-transitions":
            if len(parts) < 2:
                print("  Использование: jira-transitions <KEY>")
            else:
                cmd_jira_transitions(parts[1].upper())
        elif cmd == "jira-attachments":
            if len(parts) < 2:
                print("  Использование: jira-attachments <KEY>")
            else:
                cmd_jira_attachments(parts[1].upper())
        elif cmd == "jira-comments":
            if len(parts) < 2:
                print("  Использование: jira-comments <KEY>")
            else:
                cmd_jira_comments(parts[1].upper())
        elif cmd == "jira-dev-status":
            if len(parts) < 2:
                print("  Использование: jira-dev-status <KEY>  (экспериментально)")
            else:
                cmd_jira_dev_status(parts[1].upper())
        elif cmd == "jira-download":
            if len(parts) < 2:
                print("  Использование: jira-download <KEY> [--id=<id>] [--name=<filename>] [--out=<dir>] [--all]")
            else:
                parser = argparse.ArgumentParser(prog="jira-download", add_help=False)
                parser.add_argument("key")
                parser.add_argument("--id", dest="att_id")
                parser.add_argument("--name")
                parser.add_argument("--out")
                parser.add_argument("--all", action="store_true")
                args = parser.parse_args(parts[1:])
                cmd_jira_download(
                    args.key.upper(),
                    attachment_id=args.att_id, name=args.name,
                    out=args.out, all_files=args.all,
                )
        elif cmd == "jira-append":
            if len(parts) < 3 and not any(p.startswith("--from-file=") for p in parts[2:]):
                print("  Использование: jira-append <KEY> <text> [--from-file=<path>] [--dry-run]")
            else:
                parser = argparse.ArgumentParser(prog="jira-append", add_help=False)
                parser.add_argument("key")
                parser.add_argument("text", nargs="*")
                parser.add_argument("--from-file")
                parser.add_argument("--dry-run", action="store_true")
                args = parser.parse_args(parts[1:])
                cmd_jira_append(args.key.upper(), " ".join(args.text), from_file=args.from_file, dry_run=args.dry_run)
        elif cmd == "jira-set-description":
            if len(parts) < 3 and not any(p.startswith("--from-file=") for p in parts[2:]):
                print("  Использование: jira-set-description <KEY> <text> [--from-file=<path>] [--dry-run]")
            else:
                parser = argparse.ArgumentParser(prog="jira-set-description", add_help=False)
                parser.add_argument("key")
                parser.add_argument("text", nargs="*")
                parser.add_argument("--from-file")
                parser.add_argument("--dry-run", action="store_true")
                args = parser.parse_args(parts[1:])
                cmd_jira_set_description(args.key.upper(), " ".join(args.text), from_file=args.from_file, dry_run=args.dry_run)
        elif cmd == "jira-comment":
            if len(parts) < 3 and not any(p.startswith("--from-file=") for p in parts[2:]):
                print("  Использование: jira-comment <KEY> <text> [--from-file=<path>] [--dry-run]")
            else:
                parser = argparse.ArgumentParser(prog="jira-comment", add_help=False)
                parser.add_argument("key")
                parser.add_argument("text", nargs="*")
                parser.add_argument("--from-file")
                parser.add_argument("--dry-run", action="store_true")
                args = parser.parse_args(parts[1:])
                cmd_jira_comment(args.key.upper(), " ".join(args.text), from_file=args.from_file, dry_run=args.dry_run)
        elif cmd == "jira-transition":
            if len(parts) < 3:
                print("  Использование: jira-transition <KEY> <target> [--comment=...] [--comment-from-file=<path>] [--dry-run]")
            else:
                parser = argparse.ArgumentParser(prog="jira-transition", add_help=False)
                parser.add_argument("key")
                parser.add_argument("target")
                parser.add_argument("--comment")
                parser.add_argument("--comment-from-file")
                parser.add_argument("--dry-run", action="store_true")
                args = parser.parse_args(parts[1:])
                cmd_jira_transition(
                    args.key.upper(), args.target,
                    comment=args.comment, comment_from_file=args.comment_from_file,
                    dry_run=args.dry_run,
                )
        elif cmd == "confluence-page":
            if len(parts) < 2:
                print("  Использование: confluence-page <page_id|url>")
            else:
                cmd_confluence_page(parts[1])
        elif cmd == "confluence-update":
            if len(parts) < 3 and not any(p.startswith("--from-file=") for p in parts[2:]):
                print("  Использование: confluence-update <page_id|url> <text> [--from-file=<path>] [--title=...] [--mode=append|replace] [--dry-run]")
            else:
                parser = argparse.ArgumentParser(prog="confluence-update", add_help=False)
                parser.add_argument("page")
                parser.add_argument("text", nargs="*")
                parser.add_argument("--from-file")
                parser.add_argument("--title")
                parser.add_argument("--mode", default="append")
                parser.add_argument("--dry-run", action="store_true")
                args = parser.parse_args(parts[1:])
                cmd_confluence_update(
                    args.page, " ".join(args.text),
                    from_file=args.from_file, title=args.title,
                    mode=args.mode, dry_run=args.dry_run,
                )
        elif cmd == "confluence-create":
            parser = argparse.ArgumentParser(prog="confluence-create", add_help=False)
            parser.add_argument("text", nargs="*")
            parser.add_argument("--space", required=True)
            parser.add_argument("--title", required=True)
            parser.add_argument("--from-file")
            parser.add_argument("--parent-id")
            parser.add_argument("--dry-run", action="store_true")
            try:
                args = parser.parse_args(parts[1:])
            except SystemExit:
                print("  Использование: confluence-create --space=<SPACE> --title=<title> <text> [--from-file=<path>] [--parent-id=<id>] [--dry-run]")
                return True
            if not args.text and not args.from_file:
                print("  Передай текст страницы или --from-file=<path>")
                return True
            cmd_confluence_create(
                args.space, args.title, " ".join(args.text),
                from_file=args.from_file, parent_id=args.parent_id,
                dry_run=args.dry_run,
            )
    except Exception as e:
        print(f"  Ошибка: {e}\n")

    return True
