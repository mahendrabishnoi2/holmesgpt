import logging
from datetime import datetime, timezone

from holmes.config import Config
from holmes.core.supabase_dal import SupabaseDal
from holmes.plugins.skills.skill_loader import (
    SkillSource,
    load_filesystem_skills,
)

# How a SkillSource is labelled in the HolmesCustomSkills.source column. Every filesystem
# skill -- from a GitHub repo, inline Helm values, or a ConfigMap/Secret mount -- loads as
# SkillSource.USER and Holmes keeps no origin metadata, so they are all reported as
# "custom". Distinguishing github/inline/configmap would require new origin tagging.
# HolmesCustomSkills.status values. The column exists to surface a malformed SKILL.md, so
# both are reachable: "ok" for a skill that parsed, "error" for one that did not.
STATUS_OK = "ok"
STATUS_ERROR = "error"

SOURCE_LABELS = {
    SkillSource.USER: "custom",
    SkillSource.BUILTIN: "builtin",
}


def holmes_sync_skills_status(dal: SupabaseDal, config: Config) -> None:
    """Mirror this cluster's filesystem + builtin skills into HolmesCustomSkills.

    Purely so the UI can list them: the filesystem remains the source of truth and Holmes
    keeps executing these from disk. Runs at startup and on the periodic refresh, alongside
    holmes_sync_toolsets_status.

    Loads through load_filesystem_skills so only builtin + filesystem skills are collected --
    global and personal skills already live in HolmesRunbooks and must not be duplicated
    into the mirror. That loader also reports whether every source was readable, which is
    what makes it safe to prune on an empty result.

    Best-effort: any failure is logged and swallowed, because a display-only mirror must
    never prevent Holmes from starting.
    """
    try:
        if not config.cluster_name:
            logging.warning("Cluster name is missing; skipping custom skills sync.")
            return

        # Reports whether every skill source was readable, which the prune below depends on.
        # An empty result alone cannot distinguish "the last skill was deleted" from "the
        # ConfigMap is not mounted yet" -- and only the first should prune the mirror.
        loaded = load_filesystem_skills(config.custom_skill_paths)

        # UTC-aware: a naive timestamp would be interpreted in the database session's
        # timezone, so updated_at would not reflect the real sync time off-UTC.
        updated_at = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "account_id": dal.account_id,
                "cluster_id": config.cluster_name,
                "skill_name": skill.name,
                "source": SOURCE_LABELS.get(skill.source, skill.source.value),
                "description": skill.description,
                "content": skill.content,
                "source_path": skill.source_path,
                "status": STATUS_OK,
                "error": None,
                "updated_at": updated_at,
            }
            for skill in loaded.skills
            if skill.source in SOURCE_LABELS
        ]

        # A SKILL.md that failed to parse gets a row too. Without this the columns could
        # never hold anything but "ok": the loader drops unparseable skills, so nothing
        # broken ever reached the row builder and a user's malformed file simply vanished
        # from the UI rather than showing up as broken.
        #
        # Keyed on the same skill_name the successful row would have used (the normalized
        # directory name), so a file that starts failing replaces its own healthy row
        # instead of accumulating a second one. If a name collides with a skill that loaded
        # from a different path, the upsert is last-write-wins -- rare, and preferable to
        # dropping the error.
        rows += [
            {
                "account_id": dal.account_id,
                "cluster_id": config.cluster_name,
                "skill_name": failure.skill_name,
                "source": SOURCE_LABELS.get(
                    failure.source, SOURCE_LABELS[SkillSource.USER]
                ),
                # Nullable, and there is nothing trustworthy to put here -- the parse that
                # would have produced them is what failed.
                "description": None,
                "content": None,
                "source_path": failure.source_path,
                "status": STATUS_ERROR,
                "error": failure.error,
                "updated_at": updated_at,
            }
            for failure in loaded.failed_skills
            if failure.source in SOURCE_LABELS
        ]

        # Conservative: prune only when EVERY source was readable. A partially-readable load
        # must not delete the rows for the part that failed, and a fully-unreadable one must
        # not wipe the list -- while a clean load of zero skills genuinely means "the last
        # skill was deleted" and must prune.
        dal.sync_skills(rows, config.cluster_name, prune=loaded.sources_ok)
    except Exception:
        logging.exception("Failed to sync custom skills", exc_info=True)
