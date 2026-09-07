---
name: ad-creative-naming-organizer-source
description: >
  广告创意素材命名整理器源码仓库入口。可安装技能位于
  skills/ad-creative-naming-organizer/，完整用法见该目录下的 SKILL.md。
---

# 广告创意素材命名整理器（源码仓库）

本仓库是 Codex skill `ad-creative-naming-organizer` 的源码与发布仓库。

- 可安装技能目录：[skills/ad-creative-naming-organizer](skills/ad-creative-naming-organizer/SKILL.md)
- 完整技能说明、铁律与使用流程：读取 `skills/ad-creative-naming-organizer/SKILL.md`
- 本地开发/修改入口：`skills/ad-creative-naming-organizer/` 下的 `SKILL.md`、`agents/`、`references/`、`scripts/`
- 共享盘执行环境使用的是安装到 `~/.codex/skills/ad-creative-naming-organizer/` 的技能副本；仓库更新后需要重新安装到各台机器

给其他 Codex 用户安装时使用：

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" --repo cxx1200/ad-creative-naming-organizer --path skills/ad-creative-naming-organizer
```

仓库为私有仓库时，对方需要先获得该仓库的读取权限。
