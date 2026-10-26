"""Parse the agent's uiautomator-compatible XML into a flat list of nodes."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class Node:
    text: str
    desc: str
    rid: str
    cls: str
    package: str
    clickable: bool
    enabled: bool
    scrollable: bool
    bounds: tuple[int, int, int, int]
    depth: int
    children: list[Node] = field(default_factory=list, repr=False)

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def label(self) -> str:
        return self.text or self.desc

    def contains_y(self, y: int) -> bool:
        return self.bounds[1] <= y < self.bounds[3]

    def descendants(self) -> list[Node]:
        out: list[Node] = []
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(reversed(node.children))
        return out

    def texts(self) -> list[str]:
        return [n.text for n in [self, *self.descendants()] if n.text]


class Screen:
    """One snapshot of the active window."""

    def __init__(self, xml: str) -> None:
        self.nodes: list[Node] = []
        root = ET.fromstring(xml)
        self._walk(root, None, 0)

    def _walk(self, element: ET.Element, parent: Node | None, depth: int) -> None:
        for child in element:
            if child.tag != "node":
                continue
            a = child.attrib
            match = _BOUNDS.fullmatch(a.get("bounds", ""))
            bounds = tuple(int(v) for v in match.groups()) if match else (0, 0, 0, 0)
            node = Node(
                text=a.get("text", ""),
                desc=a.get("content-desc", ""),
                rid=a.get("resource-id", ""),
                cls=a.get("class", ""),
                package=a.get("package", ""),
                clickable=a.get("clickable") == "true",
                enabled=a.get("enabled") == "true",
                scrollable=a.get("scrollable") == "true",
                bounds=bounds,  # type: ignore[arg-type]
                depth=depth,
            )
            self.nodes.append(node)
            if parent is not None:
                parent.children.append(node)
            self._walk(child, node, depth + 1)

    @property
    def package(self) -> str:
        return self.nodes[0].package if self.nodes else ""

    def by_rid(self, rid: str) -> list[Node]:
        return [n for n in self.nodes if n.rid == rid]

    def first_rid(self, rid: str) -> Node | None:
        found = self.by_rid(rid)
        return found[0] if found else None

    def by_text(self, pattern: str) -> list[Node]:
        rx = re.compile(pattern)
        return [n for n in self.nodes if n.text and rx.fullmatch(n.text)]

    def first_text(self, pattern: str) -> Node | None:
        found = self.by_text(pattern)
        return found[0] if found else None

    def has_text(self, pattern: str) -> bool:
        return self.first_text(pattern) is not None

    def clickable_ancestor(self, node: Node) -> Node | None:
        """The innermost clickable node whose subtree holds ``node``."""
        best: Node | None = None
        for candidate in self.nodes:
            if candidate.clickable and (candidate is node or node in candidate.descendants()):
                if best is None or candidate.depth > best.depth:
                    best = candidate
        return best

    def all_texts(self) -> list[str]:
        return [n.text for n in self.nodes if n.text]
