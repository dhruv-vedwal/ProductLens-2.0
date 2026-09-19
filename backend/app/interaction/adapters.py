"""Behavior-class dispatch for visible production interactions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.contracts.models import ControlDescriptor, OperationKind, SemanticOperation


@dataclass(frozen=True)
class BehaviorAdapter:
    name: str
    behavior_classes: frozenset[str]
    operation_kinds: frozenset[OperationKind]

    async def execute(
        self,
        operation: SemanticOperation,
        descriptor: ControlDescriptor,
        dispatch: Callable[[SemanticOperation], Awaitable[Any]],
    ) -> Any:
        if descriptor.behavior_class not in self.behavior_classes:
            raise ValueError(f"{self.name} cannot operate {descriptor.behavior_class}")
        if operation.kind not in self.operation_kinds:
            raise ValueError(f"{self.name} cannot execute {operation.kind.value}")
        result = await dispatch(operation)
        if isinstance(result, dict):
            return {
                **result,
                "behavior_adapter": self.name,
                "behavior_class": descriptor.behavior_class,
                "classification_confidence": descriptor.classification_confidence,
            }
        return {
            "result": result,
            "behavior_adapter": self.name,
            "behavior_class": descriptor.behavior_class,
            "classification_confidence": descriptor.classification_confidence,
        }


class BehaviorAdapterRegistry:
    """Select a generic adapter solely from fresh observed behavior."""

    def __init__(self) -> None:
        text = frozenset(
            {
                OperationKind.FILL_TEXT,
                OperationKind.FILL_EMAIL,
                OperationKind.FILL_PHONE,
                OperationKind.SEARCH,
            }
        )
        choice = frozenset(
            {
                OperationKind.SELECT_OPTION,
                OperationKind.SELECT_DATE,
                OperationKind.SELECT_DATE_RANGE,
            }
        )
        toggle = frozenset(
            {OperationKind.CHECK, OperationKind.UNCHECK, OperationKind.CHOOSE_RADIO}
        )
        click = frozenset(
            {
                OperationKind.CLICK,
                OperationKind.OPEN_MODAL,
                OperationKind.CLOSE_MODAL,
                OperationKind.OPEN_NAVIGATION_ITEM,
                OperationKind.APPLY_FILTER,
                OperationKind.SUBMIT,
                OperationKind.CREATE_RECORD,
            }
        )
        canvas = frozenset({OperationKind.POINTER_SEQUENCE, OperationKind.DRAG})
        self._adapters = (
            BehaviorAdapter(
                "TextInputAdapter",
                frozenset({"text_input", "email_input", "phone_input", "multiline_input"}),
                text,
            ),
            BehaviorAdapter(
                "AutocompleteAdapter", frozenset({"autocomplete"}), text | choice
            ),
            BehaviorAdapter("NativeSelectAdapter", frozenset({"native_select"}), choice),
            BehaviorAdapter("ComboboxAdapter", frozenset({"combobox"}), choice),
            BehaviorAdapter(
                "DatePickerAdapter", frozenset({"native_date", "date_picker"}), choice
            ),
            BehaviorAdapter("TimeSlotAdapter", frozenset({"time_slot"}), choice | click),
            BehaviorAdapter(
                "DependentFieldAdapter",
                frozenset({"dependent_async"}),
                text | choice | toggle | click,
            ),
            BehaviorAdapter(
                "RadioCheckboxAdapter", frozenset({"radio", "checkbox"}), toggle | click
            ),
            BehaviorAdapter(
                "ModalDrawerAdapter",
                frozenset({"button", "submit", "tab", "accordion"}),
                click,
            ),
            BehaviorAdapter(
                "CanvasAdapter",
                frozenset({"canvas_tool", "canvas_surface", "drag_target", "rich_text"}),
                canvas | click | text,
            ),
        )

    def select(
        self, operation: SemanticOperation, descriptor: ControlDescriptor
    ) -> BehaviorAdapter:
        matches = [
            adapter
            for adapter in self._adapters
            if descriptor.behavior_class in adapter.behavior_classes
            and operation.kind in adapter.operation_kinds
        ]
        if len(matches) != 1:
            raise ValueError(
                "BEHAVIOR_ADAPTER_UNRESOLVED:"
                f"{descriptor.behavior_class}:{operation.kind.value}"
            )
        return matches[0]

    async def execute(
        self,
        operation: SemanticOperation,
        descriptor: ControlDescriptor,
        dispatch: Callable[[SemanticOperation], Awaitable[Any]],
    ) -> Any:
        if not descriptor.visible or not descriptor.enabled:
            raise ValueError("DEPENDENT_CONTROL_NOT_READY")
        return await self.select(operation, descriptor).execute(
            operation, descriptor, dispatch
        )
