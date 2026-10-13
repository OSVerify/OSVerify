"""Paper-only review profiles. Solution requests and their policies remain unchanged."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from functools import cache
from pathlib import Path

from osverifier.backend import ROLE_SKILLS, CodexBackend
from osverifier.frozen import frozen_executable, is_frozen
from osverifier.storage import atomic_json

from .evidence import (
    attachment_paths,
    attachment_prompt,
    check_images,
    observation_schema,
    take_observations,
)
from .image_selection import select_images

FIELDS = ("math", "ai")
FIELD_LABELS = {"math": "Mathematics", "ai": "AI / ML"}
AI_SKILLS = Path(__file__).parent / "ai_skills"
AI_REVIEW_SCOPE = (
    "Correctness review of the supplied paper text, including available tables and appendices. "
    "Novelty, significance, and publication recommendations are outside scope. "
    "Review completion does not certify correctness or empirical reproduction; "
    "claims depending on unavailable figures, code, data, or logs may remain unresolved."
)


def validate_field(value: str) -> str:
    if value not in FIELDS:
        raise ValueError("paper field must be math or ai")
    return value


@cache
def _compiled_code_sha256() -> str:
    with Path(frozen_executable()).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def code_sha256(source: Path) -> str:
    """Source code in development; the actual executable in compiled desktop builds."""
    if is_frozen():
        return _compiled_code_sha256()
    return hashlib.sha256(source.read_bytes()).hexdigest()


def profile_assets() -> dict[str, str]:
    return {"code_sha256": code_sha256(Path(__file__)),
            "image_selection_code_sha256": code_sha256(Path(__file__).with_name("image_selection.py")),
            "evidence_code_sha256": code_sha256(Path(__file__).with_name("evidence.py")), **{
        str(p.relative_to(AI_SKILLS)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(AI_SKILLS.rglob("*.md"))}}


class AIPaperBackend:
    """Route every paper role through the AI policy and freeze it in backend provenance."""

    def __init__(self, backend, images=()):
        self.backend = backend
        self.images = list(images)

    @property
    def identity(self):
        check_images(self.images)
        return {**self.backend.identity, "paper_field": "ai", "paper_profile": profile_assets(),
                "paper_images": self.images, "ephemeral": True}

    def generate_with_references(self, role, payload, schema, attempt_dir, *, reference_tools):
        return self.generate(role, payload, schema, attempt_dir, reference_tools=reference_tools)

    def generate(self, role, payload, schema, attempt_dir, *, reference_tools=None):
        context = {"field": "ai", "scope": AI_REVIEW_SCOPE}
        payload = {**payload, "paper_review": context}
        substantive = role in ("proposer", "issue_validator", "feedback_auditor")
        images = self.images if substantive else []
        check_images(images)
        attached, selection_reason = select_images(images, role, payload)
        if substantive:
            payload["paper_images"] = images
            context["attached_image_ids"] = [i["image_id"] for i in attached]
            context["image_selection_reason"] = selection_reason
            context["visual_instructions"] = attachment_prompt(images, attached)
        if role == "feedback_auditor" and "issue_review" in payload:
            # Only the validator's exact texts can be released. Earlier proposer
            # drafts remain in the proposer artifact, but are not audit candidates.
            if "proposed_issue" in payload:
                payload["proposed_issue"] = {
                    key: value for key, value in payload["proposed_issue"].items()
                    if key != "proposed_feedback"
                }
            context["feedback_candidates_path"] = "issue_review.feedback_candidates"
            context["feedback_candidate_ids"] = [
                candidate["feedback_id"] for candidate in payload["issue_review"]["feedback_candidates"]
            ]
            # Constrain generation as well as validating links after the call.
            # In particular, an empty review must not generate obsolete draft IDs.
            schema = deepcopy(schema)
            ids = context["feedback_candidate_ids"]
            decisions = schema["properties"]["decisions"]
            decisions.update(minItems=len(ids), maxItems=len(ids))
            if ids:
                schema["$defs"]["FeedbackDecision"]["properties"]["feedback_id"]["enum"] = ids
        if substantive:
            schema = observation_schema(schema, images, proposer=role == "proposer", required_images=attached)
        if isinstance(self.backend, CodexBackend):
            result = self.backend.generate_with_skills(
                role, payload, schema, attempt_dir, AI_SKILLS,
                images=attachment_paths(attached), ephemeral=True, **({"reference_tools": reference_tools} if reference_tools else {}))
        else:
            # Non-Codex backends receive the same instructions as task metadata.
            context["policy"] = (AI_SKILLS / "policy.md").read_text()
            context["role_instructions"] = (AI_SKILLS / ROLE_SKILLS[role] / "SKILL.md").read_text()
            if reference_tools and hasattr(self.backend, "generate_with_references"):
                result = self.backend.generate_with_references(
                    role, payload, schema, attempt_dir, reference_tools=reference_tools)
            else:
                result = self.backend.generate(role, payload, schema, attempt_dir)
        check_images(images)
        if substantive:
            result, observations = take_observations(result, images, proposer=role == "proposer", required_images=attached)
            atomic_json(attempt_dir / "paper-observations.json", {
                "role": role, "scope_id": payload.get("scope", {}).get("scope_id"),
                "images": images, "attached_image_ids": context["attached_image_ids"],
                "image_selection_reason": selection_reason, **observations})
        return result

    def attempt_artifacts(self, attempt_dir):
        path = attempt_dir / "paper-observations.json"
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()} if path.exists() else {}

    def cancel(self):
        cancel = getattr(self.backend, "cancel", None)
        if cancel:
            cancel()

    def halt_for_quota(self, error):
        halt = getattr(self.backend, "halt_for_quota", None)
        if halt:
            halt(error)
