from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.modules.entities.infrastructure.models import EntityFieldModel


class CalculationError(Exception):
    """Вычисляемое выражение не может быть корректно рассчитано."""


MAX_EXPRESSION_DEPTH = 32
MAX_EXPRESSION_OPERATIONS = 1_000


def evaluate_calculated_fields(
    fields: list[EntityFieldModel], values: dict[str, Any]
) -> dict[str, Any]:
    """Рассчитать поля по безопасному JSON AST без eval и выполнения кода."""

    calculated = {field.code: field for field in fields if field.formula is not None}
    result = dict(values)
    resolved: set[str] = set()
    resolving: set[str] = set()

    def resolve(code: str) -> Any:
        if code not in calculated:
            return result.get(code)
        if code in resolved:
            return result.get(code)
        if code in resolving:
            raise CalculationError("Обнаружена циклическая зависимость вычисляемых полей")
        resolving.add(code)
        try:
            value = _evaluate(
                calculated[code].formula,
                resolve,
                budget={"operations": MAX_EXPRESSION_OPERATIONS},
            )
        except (CalculationError, DivisionByZero, InvalidOperation, TypeError, ValueError):
            value = None
        resolving.remove(code)
        resolved.add(code)
        result[code] = _json_number(value)
        return result[code]

    for field_code in calculated:
        resolve(field_code)
    return result


def formula_dependencies(
    node: Any, *, depth: int = 0, budget: dict[str, int] | None = None
) -> set[str]:
    """Вернуть коды полей, на которые ссылается выражение."""

    if depth > MAX_EXPRESSION_DEPTH:
        raise CalculationError("Превышена допустимая глубина формулы")
    budget = budget or {"operations": MAX_EXPRESSION_OPERATIONS}
    budget["operations"] -= 1
    if budget["operations"] < 0:
        raise CalculationError("Формула содержит слишком много операций")
    if not isinstance(node, dict):
        raise CalculationError("Узел формулы должен быть объектом")
    if set(node) == {"field"} and isinstance(node["field"], str):
        return {node["field"]}
    if set(node) == {"value"}:
        return set()
    operation = node.get("op")
    args = node.get("args")
    if not isinstance(operation, str) or not isinstance(args, list):
        raise CalculationError("Операция формулы должна содержать op и args")
    dependencies: set[str] = set()
    for argument in args:
        dependencies.update(
            formula_dependencies(argument, depth=depth + 1, budget=budget)
        )
    return dependencies


def _evaluate(
    node: Any,
    resolve,
    *,
    depth: int = 0,
    budget: dict[str, int],
) -> Any:
    if depth > MAX_EXPRESSION_DEPTH:
        raise CalculationError("Превышена допустимая глубина формулы")
    budget["operations"] -= 1
    if budget["operations"] < 0:
        raise CalculationError("Формула содержит слишком много операций")
    if not isinstance(node, dict):
        raise CalculationError("Некорректный узел формулы")
    if set(node) == {"field"}:
        return resolve(str(node["field"]))
    if set(node) == {"value"}:
        return node["value"]
    op = node.get("op")
    raw_args = node.get("args")
    if not isinstance(op, str) or not isinstance(raw_args, list):
        raise CalculationError("Некорректная операция формулы")
    args = [
        _evaluate(argument, resolve, depth=depth + 1, budget=budget)
        for argument in raw_args
    ]
    if op == "coalesce":
        return next((value for value in args if value is not None), None)
    if op == "concat":
        return "".join("" if value is None else str(value) for value in args)
    if op == "if" and len(args) == 3:
        return args[1] if bool(args[0]) else args[2]
    if op == "not" and len(args) == 1:
        return not bool(args[0])
    if op in {"and", "or"}:
        return all(map(bool, args)) if op == "and" else any(map(bool, args))
    if op == "count":
        return sum(value is not None for value in args)
    if op in {"eq", "ne", "gt", "gte", "lt", "lte"} and len(args) == 2:
        left, right = args
        return {
            "eq": left == right,
            "ne": left != right,
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[op]
    numbers = [_decimal(value) for value in args]
    if op in {"add", "sum"}:
        return sum(numbers, Decimal(0))
    if op == "subtract" and len(numbers) == 2:
        return numbers[0] - numbers[1]
    if op == "multiply":
        result = Decimal(1)
        for number in numbers:
            result *= number
        return result
    if op == "divide" and len(numbers) == 2:
        if numbers[1] == 0:
            raise DivisionByZero
        return numbers[0] / numbers[1]
    if op == "min" and numbers:
        return min(numbers)
    if op == "max" and numbers:
        return max(numbers)
    if op == "round" and len(numbers) in {1, 2}:
        precision = int(numbers[1]) if len(numbers) == 2 else 0
        if precision < 0 or precision > 12:
            raise CalculationError("Точность ROUND должна быть от 0 до 12")
        quantum = Decimal(1).scaleb(-precision)
        return numbers[0].quantize(quantum, rounding=ROUND_HALF_UP)
    raise CalculationError(f"Операция «{op}» не поддерживается")


def _decimal(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise CalculationError("Для арифметической операции требуется число")
    return Decimal(str(value))


def _json_number(value: Any) -> Any:
    if not isinstance(value, Decimal):
        return value
    if value == value.to_integral_value():
        return int(value)
    return format(value.normalize(), "f")
