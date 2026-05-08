"""One-shot: assign domains + is_valid=False to existing wiki pages.

Step 2 of the Domain plan. Reads each known wiki page, sets domains
(controlled vocabulary) + flips is_valid=False on noise entries that
admin has confirmed as junk.

Re-runs are safe — uses WikiStore.read_page + write_page so frontmatter
fields are preserved, only the targeted fields get rewritten.
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/Users/pangwanchun/AIProjects/TudouClaw_new")
from app.knowledge.wiki_store import get_wiki_store

# (scope, kind, slug) → {domains: [...], invalidate: bool}
PLAN = [
    # global/experience
    ("global", "experience", "pci-dss-40-认证标准完整学习总结",
     {"domains": ["security", "payments-compliance", "regulatory"]}),
    ("global", "experience", "agent协作项目管理经验库",
     {"domains": ["project-management"]}),
    ("global", "experience", "用户告知当前时间",
     {"invalidate": True}),  # noise
    ("global", "experience", "s",
     {"invalidate": True}),  # noise
    # global/methodology
    ("global", "methodology", "新项目启动手册agent协作项目",
     {"domains": ["project-management"]}),
    # global/reference
    ("global", "reference", "github上精美的ui设计网站参考",
     {"domains": ["frontend", "design"]}),
    # role/general
    ("role:general", "experience",
     "gumroad和github-marketplace上架卖模板的流程与模版规范",
     {"domains": ["marketing", "content-publishing"]}),
    # role/coder
    ("role:coder", "experience", "deploying-to-production",
     {"domains": ["devops"]}),
    ("role:coder", "experience", "async-unit-tests",
     {"domains": ["testing", "backend"]}),
    ("role:coder", "experience", "writing-unit-tests-for-async-code",
     {"domains": ["testing", "backend"]}),
    ("role:coder", "experience", "test",
     {"invalidate": True}),  # noise
    ("role:coder", "experience", "project-logging-setup",
     {"domains": ["backend", "devops"]}),
]


def main():
    ws = get_wiki_store()
    updated = 0
    skipped = 0
    invalidated = 0
    for scope, kind, slug, action in PLAN:
        page = ws.read_page(scope, kind, slug)
        if page is None:
            print(f"  SKIP (not found): {scope}/{kind}/{slug}")
            skipped += 1
            continue
        changed = False
        if action.get("invalidate"):
            if page.is_valid:
                page.is_valid = False
                page.consecutive_fails = max(page.consecutive_fails, 3)
                changed = True
                invalidated += 1
                print(f"  INVALIDATE: {scope}/{kind}/{slug}")
            else:
                print(f"  already invalid: {scope}/{kind}/{slug}")
        elif "domains" in action:
            new_domains = action["domains"]
            if list(page.domains or []) != new_domains:
                page.domains = list(new_domains)
                changed = True
                updated += 1
                print(f"  DOMAINS+={new_domains}: {scope}/{kind}/{slug}")
            else:
                print(f"  domains already set: {scope}/{kind}/{slug}")
        if changed:
            ws.write_page(page, log_action="domain-classify")
    print()
    print(f"Done. updated={updated}  invalidated={invalidated}  skipped={skipped}")


if __name__ == "__main__":
    main()
