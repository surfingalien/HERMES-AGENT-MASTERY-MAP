"""skills_list (#33), skill_view (#34), skill_manage (#35, #36, #40)."""

from __future__ import annotations

from hermes.skills.manager import SkillManager
from hermes.tools.registry import ToolContext, ToolError, tool


@tool(
    "skills_list",
    "skills",
    "List available skills (reusable procedures) with their descriptions, optionally filtered by category/keyword.",
    {"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": "string"}}},
)
def skills_list(args: dict, ctx: ToolContext):
    q = (args.get("query") or "").lower()
    cat = args.get("category")
    out = []
    for s in SkillManager(ctx.config).discover():
        if cat and s.category != cat:
            continue
        if q and q not in f"{s.name} {s.description}".lower():
            continue
        out.append({"name": s.name, "category": s.category, "description": s.description})
    return {"skills": out, "count": len(out)}


@tool(
    "skill_view",
    "skills",
    "Load a skill's full instructions (SKILL.md). Pass file_path to load a supporting file "
    "(references/, templates/, scripts/) listed in the skill.",
    {"type": "object", "properties": {"name": {"type": "string"}, "file_path": {"type": "string"}}, "required": ["name"]},
)
def skill_view(args: dict, ctx: ToolContext):
    res = SkillManager(ctx.config).view(args["name"], args.get("file_path"))
    if ctx.agent is not None and "error" not in res:
        ctx.agent.skills_used.add(args["name"])
    return res


@tool(
    "skill_manage",
    "skills",
    "Create and maintain skills - your procedural memory. Create a skill after completing a complex task "
    "(many tool calls, tricky fixes, a workflow the user will want again). Patch a skill when you find it "
    "outdated or wrong while using it. SKILL.md needs YAML frontmatter with name + description, then "
    "sections like: When to use, Steps, Pitfalls, Verification.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"]},
            "name": {"type": "string"},
            "content": {"type": "string", "description": "Full SKILL.md (create/edit) or file content (write_file)"},
            "category": {"type": "string", "description": "Folder to group the skill under (create)"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "file_path": {"type": "string", "description": "e.g. references/api.md (write_file/remove_file/patch)"},
        },
        "required": ["action", "name"],
    },
    parallel_safe=False,
)
def skill_manage(args: dict, ctx: ToolContext):
    mgr = SkillManager(ctx.config)
    action, name = args["action"], args["name"]
    if action == "create":
        res = mgr.create(name, args.get("content") or "", args.get("category") or "")
    elif action == "edit":
        res = mgr.edit(name, args.get("content") or "")
    elif action == "patch":
        res = mgr.patch(name, args.get("old_string") or "", args.get("new_string") or "", args.get("file_path"))
    elif action == "delete":
        res = mgr.delete(name)
    elif action == "write_file":
        res = mgr.write_file(name, args.get("file_path") or "", args.get("content") or "")
    elif action == "remove_file":
        res = mgr.remove_file(name, args.get("file_path") or "")
    else:
        raise ToolError(f"unknown action {action}")
    if ctx.agent is not None and res.get("ok"):
        ctx.agent.on_skill_changed()
    return res
