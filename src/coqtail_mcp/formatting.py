"""Turn the structured :class:`xmlInterface.Goals` into plain text.

Coqtail's pretty-printer (``coqtail.Coqtail.pp_goals``) targets a vim panel
with per-character highlight positions. For an AI-agent context, plain text
is more useful and much cheaper to pass through — so we strip the richpp
tags and emit something close to what ``coqtop``'s CLI would show.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple, TypeVar, Union

from . import _COQTAIL_LIB_DIR  # noqa: F401

from xmlInterface import Goals, TaggedToken, join_tagged_tokens  # type: ignore


TextLike = Union[str, Sequence[TaggedToken]]
T = TypeVar("T")


def _flatten(text: TextLike) -> str:
    """Extract the plain string from a richpp token stream (or str)."""
    if isinstance(text, str):
        return text
    return join_tagged_tokens(text)


def _resolve_line_index(index: int, total_lines: int) -> int:
    """Resolve a 1-indexed or negative-from-bottom line reference."""
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValueError("range entries must be integers")
    if index == 0:
        raise ValueError("range entries must be non-zero")
    if index < 0:
        return total_lines + index + 1
    return index


def apply_line_range(
    text: str,
    line_range: Optional[Sequence[int]],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return ``text`` restricted to an inclusive line range.

    Positive indexes are 1-based. Negative indexes count from the bottom, so
    ``[-5, -1]`` selects the final five rendered lines.
    """
    if line_range is None:
        return text, None
    if len(line_range) != 2:
        raise ValueError("range must contain exactly two entries: [start, end]")

    lines = text.splitlines()
    total_lines = len(lines)
    raw_start, raw_end = line_range
    start = _resolve_line_index(raw_start, total_lines)
    end = _resolve_line_index(raw_end, total_lines)
    if start > end:
        raise ValueError("range start must be <= range end after resolution")

    clipped_start = max(start, 1)
    clipped_end = min(end, total_lines)
    if total_lines == 0 or clipped_start > clipped_end:
        selected = []
        selected_range = None
    else:
        selected = lines[clipped_start - 1 : clipped_end]
        selected_range = [clipped_start, clipped_end]

    ranged_text = "\n".join(selected)
    if selected:
        ranged_text += "\n"

    return ranged_text, {
        "requested": [raw_start, raw_end],
        "resolved": [start, end],
        "selected": selected_range,
        "total_lines": total_lines,
        "truncated": selected_range != [1, total_lines],
    }


def apply_item_range(
    items: Sequence[T],
    item_range: Optional[Sequence[int]],
) -> List[T]:
    """Return ``items`` restricted to an inclusive 1-indexed range.

    This shares the public ``range`` convention used by the MCP tools:
    positive indexes are 1-based and negative indexes count from the end.
    """
    if item_range is None:
        return list(items)
    if len(item_range) != 2:
        raise ValueError("range must contain exactly two entries: [start, end]")

    total_items = len(items)
    raw_start, raw_end = item_range
    start = _resolve_line_index(raw_start, total_items)
    end = _resolve_line_index(raw_end, total_items)
    if start > end:
        raise ValueError("range start must be <= range end after resolution")

    clipped_start = max(start, 1)
    clipped_end = min(end, total_items)
    if total_items == 0 or clipped_start > clipped_end:
        return []
    return list(items[clipped_start - 1 : clipped_end])


def truncate_strings(value: Any, max_chars: Optional[int]) -> Any:
    """Recursively cap every string value to ``max_chars`` characters."""
    if max_chars is None:
        return value
    if isinstance(value, str):
        return value[:max_chars]
    if isinstance(value, list):
        return [truncate_strings(item, max_chars) for item in value]
    if isinstance(value, tuple):
        return tuple(truncate_strings(item, max_chars) for item in value)
    if isinstance(value, dict):
        return {
            key: truncate_strings(item, max_chars)
            for key, item in value.items()
        }
    return value


def format_goals(goals: Optional[Goals]) -> str:
    """Render a Coqtail :class:`Goals` object as a single plain-text block."""
    if goals is None:
        return "No proof in progress."

    ngoals = len(goals.fg)
    nhidden = len(goals.bg[0]) if goals.bg else 0
    nshelved = len(goals.shelved)
    nadmit = len(goals.given_up)

    lines: List[str] = []
    lines.append(f"{ngoals} subgoal{'' if ngoals == 1 else 's'}")
    if nhidden > 0:
        lines.append(f"({nhidden} unfocused at this level)")
    if nshelved or nadmit:
        parts = []
        if nshelved:
            parts.append(f"{nshelved} shelved")
        if nadmit:
            parts.append(f"{nadmit} admitted")
        lines.append(" ".join(parts))
    lines.append("")

    if ngoals == 0:
        # Coqtail-style: if there's a next goal waiting, print its conclusion;
        # otherwise declare the proof done.
        next_goal = next((bgs[0] for bgs in goals.bg if bgs), None)
        if next_goal is not None:
            label = "Next goal"
            if next_goal.name is not None:
                label += f" [{next_goal.name}]"
            lines.extend([f"{label}:", ""])
            lines.extend(_flatten(next_goal.ccl).splitlines() or [""])
        else:
            lines.append("All goals completed.")
        return "\n".join(lines).rstrip() + "\n"

    for idx, goal in enumerate(goals.fg):
        if idx == 0:
            # Print the hypothesis environment only for the first goal, just
            # like ``coqtop`` and Coqtail.
            for hyp in goal.hyp:
                lines.extend(_flatten(hyp).splitlines() or [""])

        hbar = f"{'':=>25} ({idx + 1} / {ngoals})"
        if goal.name is not None:
            hbar += f" [{goal.name}]"
        lines.extend(["", hbar, ""])
        lines.extend(_flatten(goal.ccl).splitlines() or [""])

    return "\n".join(lines).rstrip() + "\n"


def summarize_goals(
    goals: Optional[Goals],
    *,
    include_details: bool = True,
    hypothesis_range: Optional[Sequence[int]] = None,
) -> dict:
    """A structured summary alongside the plain-text view.

    Useful when the agent wants counts or the raw hypothesis/conclusion
    strings without re-parsing the text block. ``hypothesis_range`` limits
    the hypothesis entries included for each focused goal.
    """
    if goals is None:
        return {
            "in_proof": False,
            "fg": [],
            "bg_count": 0,
            "shelved": 0,
            "given_up": 0,
        }

    if not include_details:
        return {
            "in_proof": True,
            "fg": [
                _omit_empty(
                    {
                        "name": g.name,
                        "hypothesis_count": len(g.hyp),
                        "conclusion_line_count": len(
                            _flatten(g.ccl).splitlines() or [""]
                        ),
                    }
                )
                for g in goals.fg
            ],
            "details_included": False,
            "bg_count": sum(len(level) for level in goals.bg),
            "shelved": len(goals.shelved),
            "given_up": len(goals.given_up),
        }

    return {
        "in_proof": True,
        "fg": [
            _summarize_goal(g, hypothesis_range)
            for g in goals.fg
        ],
        "bg_count": sum(len(level) for level in goals.bg),
        "shelved": len(goals.shelved),
        "given_up": len(goals.given_up),
    }


def _summarize_goal(goal: Any, hypothesis_range: Optional[Sequence[int]]) -> dict:
    hypotheses = [_flatten(h) for h in goal.hyp]
    summary = {
        "name": goal.name,
        "hypotheses": apply_item_range(hypotheses, hypothesis_range),
        "conclusion": _flatten(goal.ccl),
    }
    if hypothesis_range is not None:
        summary["hypothesis_count"] = len(hypotheses)
    return _omit_empty(summary)


def _omit_empty(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop absent optional values from compact public summaries."""
    return {k: v for k, v in data.items() if v is not None and v != ""}
