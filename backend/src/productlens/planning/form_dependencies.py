"""Evidence-driven ordering for dependent form controls."""

from __future__ import annotations

from productlens.contracts.models import FormField


class FormDependencyError(ValueError):
    """The observed form dependency graph cannot be executed safely."""


def order_form_fields(fields: list[FormField] | tuple[FormField, ...]) -> list[FormField]:
    """Topologically order observed fields while preserving discovery order.

    Dependencies are semantic field names captured by discovery. Unknown
    dependencies and cycles fail closed instead of guessing an action order.
    """

    ordered = list(fields)
    by_name = {field.name.casefold(): field for field in ordered}
    position = {field.name.casefold(): index for index, field in enumerate(ordered)}
    for field in ordered:
        for dependency in field.depends_on:
            if dependency.casefold() not in by_name:
                raise FormDependencyError(
                    f"Field {field.name!r} depends on an unobserved field {dependency!r}"
                )
    visiting: set[str] = set()
    visited: set[str] = set()
    result: list[FormField] = []

    def visit(name: str) -> None:
        if name in visiting:
            raise FormDependencyError("Observed form dependency graph contains a cycle")
        if name in visited:
            return
        visiting.add(name)
        field = by_name[name]
        for dependency in sorted(
            (item.casefold() for item in field.depends_on), key=position.__getitem__
        ):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        result.append(field)

    for field in ordered:
        visit(field.name.casefold())
    return result
