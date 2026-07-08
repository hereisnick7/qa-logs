"""
Config loader — single point of truth for reading `config/`.

Avoids re-implementing the lookup in every module / CLI command. All callers
should go through this module; nothing reads `config/*.yaml` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from dotenv import dotenv_values

from shared.paths import config_dir, workflow_home


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when config is missing or malformed."""


# ---------------------------------------------------------------------------
# Workflow-level config
# ---------------------------------------------------------------------------

def load_workflow_config() -> dict:
    """Read config/workflow.yaml + config/workflow.env. Returns merged dict."""
    cfg = config_dir()
    yaml_path = cfg / "workflow.yaml"
    env_path = cfg / "workflow.env"

    data: dict = {}
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    if env_path.exists():
        env = dotenv_values(env_path)
        if env.get("LANGUAGE"):
            data.setdefault("language", env["LANGUAGE"])
        if env.get("DEFAULT_PROJECT"):
            data.setdefault("default_project", env["DEFAULT_PROJECT"])

    return data


def load_policies() -> dict:
    """Read config/policies.yaml or fall back to legacy shared/config.py constants."""
    cfg = config_dir()
    yaml_path = cfg / "policies.yaml"
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    # Legacy fallback — shared/config.py literals.
    from shared import config as _legacy
    return {
        "protected_refs": sorted(_legacy.PROTECTED_REFS),
        "protected_ref_prefixes": list(_legacy.PROTECTED_REF_PREFIXES),
        "protected_environments": sorted(_legacy.PROTECTED_ENVIRONMENTS),
        "allowed_qa_slots": sorted(_legacy.ALLOWED_QA_SLOTS),
    }


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@dataclass
class GitLabBinding:
    host: Optional[str] = None
    project_id: Optional[int] = None
    account: str = "default"


@dataclass
class JiraBinding:
    project_key: Optional[str] = None
    account: str = "default"


@dataclass
class SentryBinding:
    project_slug: Optional[str] = None
    account: str = "default"


@dataclass
class VcsBinding:
    """
    Provider-agnostic VCS binding (Epic A — multi-provider).

    `provider` selects the implementation (gitlab | github | bitbucket); the
    credential env module dir == the provider name (config/<provider>/.env).
    `account` selects the named account within that provider's env, exactly
    like the per-module account scheme.

    Repo identity is provider-shaped:
      - gitlab:    project_id (numeric)
      - github:    owner + repo
      - bitbucket: owner (workspace) + repo (repo_slug)
    `host` is an optional self-hosted override (GitLab host / GHE host).

    For backward compatibility a project with only a legacy `gitlab:` block and
    no `vcs:` block gets a VcsBinding derived from it (provider="gitlab").
    """
    provider: str = "gitlab"
    account: str = "default"
    project_id: Optional[int] = None
    owner: Optional[str] = None
    repo: Optional[str] = None
    host: Optional[str] = None


@dataclass
class TesterBinding:
    config: Optional[str] = None


@dataclass
class ProfileBinding:
    """
    Operational/stack profile — the machine-readable knobs agents execute
    without judgement, kept out of the stack-agnostic agent contracts.

    - languages:       stack tags, e.g. ["ts", "vue"] or ["php", "js"].
    - dev_server:      {"cmd": "...", "ready_url": "...", "port": int} — how
                       code-tester brings the app up locally (replaces the
                       hardcoded `npm run dev`).
    - checks:          [{"tool": "eslint", "globs": ["*.ts","*.vue"]}, ...] —
                       local-checks command set per stack (eslint/tsc/prettier,
                       phpcs/phpstan, …).
    - browser_testable: tri-state. True/False forces the browser-tester
                       capability; None = let the planner decide from States.
    - token_search:    optional hint command for finding design tokens
                       (e.g. "rtk grep '\\$' src/" for SCSS).
    """
    languages: list = field(default_factory=list)
    dev_server: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    browser_testable: Optional[bool] = None
    token_search: Optional[str] = None


@dataclass
class Project:
    slug: str
    name: str
    repo_path: Path
    style_guide: Path
    default_branch: str = "main"
    mr_title_rule: str = "branch-name"
    gitlab: GitLabBinding = field(default_factory=GitLabBinding)
    vcs: VcsBinding = field(default_factory=VcsBinding)
    jira: JiraBinding = field(default_factory=JiraBinding)
    sentry: SentryBinding = field(default_factory=SentryBinding)
    tester: TesterBinding = field(default_factory=TesterBinding)
    profile: ProfileBinding = field(default_factory=ProfileBinding)
    qa_slots: list = field(default_factory=list)
    alt_repo_paths: dict = field(default_factory=dict)

    def resolve_repo_path(self, alias: Optional[str] = None) -> Path:
        """Repo path for an explicit location alias (e.g. "work2").

        `None`/empty/"default" → the canonical `repo_path`. A known alias from
        `alt_repo_paths` → that path. Unknown alias raises so a typo never
        silently falls back to the default checkout.
        """
        if not alias or alias == "default":
            return self.repo_path
        try:
            return self.alt_repo_paths[alias]
        except KeyError:
            known = ", ".join(sorted(self.alt_repo_paths)) or "(none)"
            raise ConfigError(
                f"project '{self.slug}' has no alt_repo_paths entry '{alias}' "
                f"(known: {known})"
            )


def _to_abs_path(value: Any) -> Path:
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = workflow_home() / p
    return p


def _parse_vcs_binding(vcs_raw: dict, gitlab_raw: dict) -> VcsBinding:
    """
    Build the VcsBinding for a project.

    Precedence:
      1. explicit `vcs:` block — provider + repo identity as given;
      2. legacy `gitlab:` block only — derive a gitlab-provider binding so
         existing single-provider configs keep working untouched;
      3. neither — empty gitlab-provider binding (default).
    """
    if vcs_raw:
        return VcsBinding(
            provider=str(vcs_raw.get("provider") or "gitlab").lower(),
            account=str(vcs_raw.get("account") or "default"),
            project_id=vcs_raw.get("project_id"),
            owner=vcs_raw.get("owner"),
            repo=vcs_raw.get("repo"),
            host=vcs_raw.get("host"),
        )
    return VcsBinding(
        provider="gitlab",
        account=str(gitlab_raw.get("account") or "default"),
        project_id=gitlab_raw.get("project_id"),
        host=gitlab_raw.get("host"),
    )


def _parse_project(raw: dict) -> Project:
    if not raw.get("slug"):
        raise ConfigError(f"project entry missing 'slug': {raw}")
    gitlab_raw = raw.get("gitlab") or {}
    vcs_raw = raw.get("vcs") or {}
    jira_raw = raw.get("jira") or {}
    sentry_raw = raw.get("sentry") or {}
    tester_raw = raw.get("tester") or {}
    profile_raw = raw.get("profile") or {}

    def _require_abs(field_name: str, value: str) -> None:
        if value and not (
            value.startswith(("/", "~"))
            or Path(value).is_absolute()  # Windows drive-letter paths (C:/..., D:\...)
        ):
            raise ConfigError(
                f"project '{raw['slug']}' {field_name} must be absolute or start "
                f"with ~ (product repos live outside WORKFLOW_HOME); got: {value!r}"
            )

    repo_raw = str(raw.get("repo_path") or "")
    _require_abs("repo_path", repo_raw)

    alt_repo_raw = raw.get("alt_repo_paths") or {}
    alt_repo_paths: dict = {}
    for alias, val in alt_repo_raw.items():
        val_s = str(val or "")
        _require_abs(f"alt_repo_paths.{alias}", val_s)
        alt_repo_paths[str(alias)] = _to_abs_path(val_s or ".")

    return Project(
        slug=str(raw["slug"]),
        name=str(raw.get("name") or raw["slug"]),
        repo_path=_to_abs_path(repo_raw or "."),
        style_guide=_to_abs_path(raw.get("style_guide") or ""),
        default_branch=str(raw.get("default_branch") or "main"),
        mr_title_rule=str(raw.get("mr_title_rule") or "branch-name"),
        gitlab=GitLabBinding(
            host=gitlab_raw.get("host"),
            project_id=gitlab_raw.get("project_id"),
            account=str(gitlab_raw.get("account") or "default"),
        ),
        vcs=_parse_vcs_binding(vcs_raw, gitlab_raw),
        jira=JiraBinding(
            project_key=jira_raw.get("project_key"),
            account=str(jira_raw.get("account") or "default"),
        ),
        sentry=SentryBinding(
            project_slug=sentry_raw.get("project_slug"),
            account=str(sentry_raw.get("account") or "default"),
        ),
        alt_repo_paths=alt_repo_paths,
        tester=TesterBinding(config=tester_raw.get("config")),
        profile=ProfileBinding(
            languages=list(profile_raw.get("languages") or []),
            dev_server=dict(profile_raw.get("dev_server") or {}),
            checks=list(profile_raw.get("checks") or []),
            browser_testable=profile_raw.get("browser_testable"),
            token_search=profile_raw.get("token_search"),
        ),
        qa_slots=list(raw.get("qa_slots") or []),
    )


def load_projects() -> list[Project]:
    """Parse config/projects.yaml. Returns empty list if no projects defined."""
    path = config_dir() / "projects.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `workflow init` or create it from "
            f"config/example/projects.yaml.example"
        )
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    raw_list = data.get("projects") or []
    return [_parse_project(p) for p in raw_list]


def get_project(
    slug: Optional[str] = None,
    jira_key: Optional[str] = None,
    repo_path: Optional[Path] = None,
    gitlab_project_id: Optional[int] = None,
    sentry_project_slug: Optional[str] = None,
    vcs_owner_repo: Optional[str] = None,
) -> Optional[Project]:
    """Resolve active project. First non-None argument wins."""
    try:
        projects = load_projects()
    except FileNotFoundError:
        return None
    if slug:
        for p in projects:
            if p.slug == slug:
                return p
        return None
    if jira_key:
        prefix = jira_key.split("-")[0].upper()
        for p in projects:
            if p.jira.project_key and p.jira.project_key.upper() == prefix:
                return p
        return None
    if gitlab_project_id is not None:
        target_id = int(gitlab_project_id)
        for p in projects:
            gid = p.gitlab.project_id
            # Also match a gitlab-provider vcs binding (projects configured via an
            # explicit `vcs:` block carry project_id there, not in legacy `gitlab:`).
            vid = p.vcs.project_id if p.vcs.provider == "gitlab" else None
            if (gid is not None and int(gid) == target_id) or (vid is not None and int(vid) == target_id):
                return p
        return None
    if vcs_owner_repo:
        target = vcs_owner_repo.lower()
        for p in projects:
            if p.vcs.owner and p.vcs.repo:
                if f"{p.vcs.owner}/{p.vcs.repo}".lower() == target:
                    return p
        return None
    if sentry_project_slug:
        for p in projects:
            if p.sentry.project_slug and p.sentry.project_slug == sentry_project_slug:
                return p
        return None
    if repo_path:
        target = Path(repo_path).expanduser().resolve()
        for p in projects:
            try:
                if p.repo_path.resolve() == target:
                    return p
            except OSError:
                continue
        return None
    return None


def resolve_project_for_file(file_path: Any) -> Optional[Project]:
    """Resolve the most-specific project that owns a touched file.

    Submodules are treated as independent projects: each is its own entry in
    `projects.yaml` with its own `repo_path` (a path nested inside a host repo's
    working tree when the submodule is checked out there). A touched file is
    matched against every project's `repo_path` and the **longest matching
    prefix wins**, so a file under a nested submodule resolves to the submodule
    project (its own style-guide + profile), not the host — without any explicit
    submodule mapping.

    Example: with `host-app` at `/work/host-app` and its submodule `web-client`
    registered at `/work/host-app/web-client`, the file
    `/work/host-app/web-client/src/App.tsx` resolves to `web-client`, while
    `/work/host-app/php-host/index.php` resolves to `host-app`.

    Returns None if no project's `repo_path` is an ancestor of the file.
    """
    try:
        projects = load_projects()
    except FileNotFoundError:
        return None

    target = Path(str(file_path)).expanduser()
    try:
        target = target.resolve()
    except OSError:
        target = target.absolute()

    best: Optional[Project] = None
    best_specificity = -1
    for p in projects:
        try:
            root = p.repo_path.resolve()
        except OSError:
            root = p.repo_path
        if target != root:
            try:
                target.relative_to(root)
            except ValueError:
                continue
        # More path components == more specific (nested) repo_path wins.
        specificity = len(root.parts)
        if specificity > best_specificity:
            best_specificity = specificity
            best = p
    return best


def resolve_account(
    module: str,
    *,
    explicit: Optional[str] = None,
    jira_key: Optional[str] = None,
    slug: Optional[str] = None,
    repo_path: Optional[Path] = None,
    gitlab_project_id: Optional[int] = None,
    sentry_project_slug: Optional[str] = None,
) -> str:
    """
    Resolve the account name for a `<module>` call.

    Precedence (first hit wins):
      1. `explicit` argument (caller already knows the account);
      2. `WORKFLOW_ACCOUNT` env var (session-level pin);
      3. active project's `<module>.account`, resolved from any of the
         project-context args (jira_key / slug / repo_path / gitlab_project_id /
         sentry_project_slug);
      4. `"default"`.
    """
    if explicit:
        return explicit
    pinned = os.getenv("WORKFLOW_ACCOUNT")
    if pinned:
        return pinned
    project = get_project(
        slug=slug,
        jira_key=jira_key,
        repo_path=repo_path,
        gitlab_project_id=gitlab_project_id,
        sentry_project_slug=sentry_project_slug,
    )
    if project is not None:
        binding = getattr(project, module, None)
        account = getattr(binding, "account", None)
        if account:
            return account
    return "default"


def resolve_provider(
    *,
    explicit: Optional[str] = None,
    jira_key: Optional[str] = None,
    slug: Optional[str] = None,
    repo_path: Optional[Path] = None,
    gitlab_project_id: Optional[int] = None,
    vcs_owner_repo: Optional[str] = None,
) -> str:
    """
    Resolve the VCS provider (gitlab | github | bitbucket) for a call.

    Mirrors `resolve_account` precedence:
      1. `explicit` argument;
      2. `WORKFLOW_PROVIDER` env var (session-level pin);
      3. active project's `vcs.provider`, resolved from any project-context arg;
      4. `"gitlab"` (the original single-provider default).
    """
    if explicit:
        return explicit.lower()
    pinned = os.getenv("WORKFLOW_PROVIDER")
    if pinned:
        return pinned.lower()
    project = get_project(
        slug=slug,
        jira_key=jira_key,
        repo_path=repo_path,
        gitlab_project_id=gitlab_project_id,
        vcs_owner_repo=vcs_owner_repo,
    )
    if project is not None and project.vcs.provider:
        return project.vcs.provider.lower()
    return "gitlab"


# ---------------------------------------------------------------------------
# Module env / accounts
# ---------------------------------------------------------------------------

def load_module_env(module: str, account: str = "default") -> dict[str, str]:
    """
    Return environment for `<module>` + `<account>`.

    Lookup order:
      1. config/<module>/.env  — base file (always loaded)
      2. process env           — explicit exports trump file values (CI)
      3. account-prefixed keys (Phase 7): `<MODULE>__<ACCOUNT>__<VAR>` is
         promoted to bare `<MODULE>_<VAR>` *last*, so the selected account
         always wins — even over a bare key that leaked into process env via a
         module-level `load_dotenv`. This is what makes parallel multi-account
         (Acme + Company B) work without manual env juggling.

    Single-account setups (no `<MODULE>__<ACCOUNT>__` keys) are unaffected:
    promotion is a no-op and process-env-over-file holds.
    """
    cfg = config_dir()
    env_path = cfg / module / ".env"
    legacy_path = workflow_home() / "modules" / module / ".env"

    merged: dict[str, str] = {}
    if env_path.exists():
        merged.update({k: v for k, v in dotenv_values(env_path).items() if v is not None})
    elif legacy_path.exists():
        merged.update({k: v for k, v in dotenv_values(legacy_path).items() if v is not None})

    # Process env trumps file values, so explicit exports keep working in CI.
    for key, value in os.environ.items():
        if value is not None:
            merged[key] = value

    # Account promotion runs LAST: <MODULE>__<ACCOUNT>__VAR -> <MODULE>_VAR.
    # Scanning the post-overlay dict means prefixed keys from either the file
    # or the process env are honoured, and the selected account wins over any
    # bare key (file or leaked-into-env).
    if account:
        prefix = f"{module.upper()}__{account.upper()}__"
        for key, value in list(merged.items()):
            if key.startswith(prefix):
                short = f"{module.upper()}_{key[len(prefix):]}"
                merged[short] = value

    return merged


def list_accounts(module: str) -> list[str]:
    """Return distinct account names defined in config/<module>/.env (Phase 7 schema)."""
    cfg = config_dir()
    env_path = cfg / module / ".env"
    if not env_path.exists():
        return ["default"]
    accounts: set[str] = set()
    prefix = f"{module.upper()}__"
    for key in dotenv_values(env_path).keys():
        if key.startswith(prefix):
            rest = key[len(prefix):]
            account = rest.split("__", 1)[0]
            if account:
                accounts.add(account.lower())
    return sorted(accounts) or ["default"]
