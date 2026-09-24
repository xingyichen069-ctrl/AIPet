"""Prepare one conversation turn before it enters the model/provider loop.

The desktop, QQ bridge and background tasks may use different system text, but
they should all cross the same boundary: resolve the per-request options once,
freeze the recent history, and hand the provider a ready system message.  This
module deliberately does not know how an HTTP request or a tool call is sent.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import memory as M
import thinking as T
import persona_runtime as PR


@dataclass(frozen=True)
class PreparedRequest:
    """A request snapshot shared by every provider/model call in one turn."""

    query: str
    history: tuple[dict, ...]
    system: str
    options: dict
    max_tokens: int | None = None
    examples: tuple[dict, ...] = ()

    def messages(self) -> list[dict]:
        messages = [{"role": "system", "content": self.system}]
        messages.extend(self.examples)
        messages.extend(self.history)
        messages.append({"role": "user", "content": self.query})
        return messages


def snapshot_options(query: str, level: str | None = None,
                     options: dict | None = None) -> dict:
    """Return a private copy of the options used by this request."""
    source = options if options is not None else T.apply_to_memory(level, query)
    return deepcopy(source)


def desktop_system(query: str, options: dict) -> str:
    """Build the normal desktop system prompt from an existing snapshot."""
    body = T.system_block(query, resolved=options) + "\n\n" + M.build_context(
        query, retrieval=options["retrieval"])
    from companion import Store, task_description
    if (M.ROOT / "data" / "companion.sqlite3").exists():
        agreements = Store(M.ROOT).tasks()
        if agreements:
            body += "\n\n## 已保存的约定与陪伴\n" + "\n".join(
                task_description(t) for t in agreements[:20])
    body += "\n\n开头的示例对话只示范语气，不是本次会话经历。"
    return f"{body}\n\n---\n\n{M.persona_text()}"


def prepare(query: str, history: list[dict] | None = None,
            level: str | None = None, system: str | None = None,
            max_tokens: int | None = None,
            options: dict | None = None) -> PreparedRequest:
    """Prepare a turn exactly once, preserving a custom system when supplied."""
    snapshot = snapshot_options(query, level, options)
    limit = 80 if snapshot.get("level") == "thunder" else 24
    frozen_history = tuple(
        {"role": item["role"], "content": item["content"]}
        for item in (history or [])[-limit:]
    )
    use_examples = system is None or snapshot.get("persona_examples", False)
    if system is None:
        system = desktop_system(query, snapshot)
    return PreparedRequest(query=query, history=frozen_history, system=system,
                           options=snapshot, max_tokens=max_tokens,
                           examples=tuple(PR.examples(M.ROOT)) if use_examples else ())
